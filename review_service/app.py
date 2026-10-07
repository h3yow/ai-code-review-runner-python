"""FastAPI HTTP layer exposing the review service."""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from review_service.models import (
    CapacityConfig,
    OverflowPolicy,
    ScopeConfig,
    TaskRecord,
)
from review_service.scheduler import QueueFullError
from review_service.service import ReviewService
from review_service.store import IdempotencyConflictError


class ScopePayload(BaseModel):
    recursive: bool = True
    include_dirs: list[str] = []
    include_names: list[str] = []
    include_suffixes: list[str] = []
    exclude_patterns: list[str] = []


class CapacityPayload(BaseModel):
    max_files: int = Field(default=10_000, ge=0)
    max_total_bytes: int = Field(default=10 * 1024 * 1024, ge=0)
    overflow_policy: Literal["reject", "truncate"] = "reject"


class SubmitRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    repo_path: str = Field(min_length=1)
    rules: list[str] = []
    scope: ScopePayload | None = None
    capacity: CapacityPayload | None = None


def _scope_to_json(scope: ScopeConfig) -> dict:
    return {
        "recursive": scope.recursive,
        "include_dirs": list(scope.include_dirs),
        "include_names": list(scope.include_names),
        "include_suffixes": list(scope.include_suffixes),
        "exclude_patterns": list(scope.exclude_patterns),
    }


def _file_result_to_json(fr) -> dict:
    return {
        "path": fr.path,
        "disposition": fr.disposition.value,
        "findings": fr.findings,
        "fail_reason": fr.fail_reason.value if fr.fail_reason else None,
        "fail_detail": fr.fail_detail,
        "excluded_by": fr.excluded_by,
        "size_bytes": fr.size_bytes,
    }


def _to_json(record: TaskRecord) -> dict:
    return {
        "task_id": record.task_id,
        "idempotency_key": record.idempotency_key,
        "repo_path": record.request.repo_path,
        "rules": sorted(record.request.rules),
        "scope": _scope_to_json(record.request.scope),
        "capacity": {
            "max_files": record.request.capacity.max_files,
            "max_total_bytes": record.request.capacity.max_total_bytes,
            "overflow_policy": record.request.capacity.overflow_policy.value,
        },
        "state": record.state.value,
        "error_category": record.error_category.value if record.error_category else None,
        "error_detail": record.error_detail,
        "findings": record.findings,
        "file_results": [_file_result_to_json(f) for f in record.file_results],
        "summary": (
            {
                "total_in_scope": record.summary.total_in_scope,
                "reviewed": record.summary.reviewed,
                "excluded": record.summary.excluded,
                "failed": record.summary.failed,
                "skipped": record.summary.skipped,
                "truncated": record.summary.truncated,
                "rejected_by_limit": record.summary.rejected_by_limit,
                "has_skips_or_failures": record.summary.has_skips_or_failures,
            }
            if record.summary is not None
            else None
        ),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def create_app(service: ReviewService) -> FastAPI:
    app = FastAPI(title="ai-code-review-runner")

    @app.post("/tasks", status_code=status.HTTP_201_CREATED)
    async def submit_task(req: SubmitRequest, response: Response) -> dict:
        scope = (
            ScopeConfig(
                recursive=req.scope.recursive,
                include_dirs=tuple(req.scope.include_dirs),
                include_names=tuple(req.scope.include_names),
                include_suffixes=tuple(req.scope.include_suffixes),
                exclude_patterns=tuple(req.scope.exclude_patterns),
            )
            if req.scope is not None
            else None
        )
        capacity = (
            CapacityConfig(
                max_files=req.capacity.max_files,
                max_total_bytes=req.capacity.max_total_bytes,
                overflow_policy=OverflowPolicy(req.capacity.overflow_policy),
            )
            if req.capacity is not None
            else None
        )
        try:
            record, created = await service.submit(
                req.idempotency_key,
                req.repo_path,
                tuple(req.rules),
                scope=scope,
                capacity=capacity,
            )
        except IdempotencyConflictError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "reason": "IDEMPOTENCY_CONFLICT",
                    "message": str(exc),
                    "existing_task_id": exc.existing_task_id,
                },
            ) from exc
        except QueueFullError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"reason": "CAPACITY_EXHAUSTED", "message": str(exc)},
            ) from exc
        if not created:
            response.status_code = status.HTTP_200_OK
        return _to_json(record)

    @app.get("/tasks")
    async def list_tasks() -> list[dict]:
        return [_to_json(r) for r in await service.list_tasks()]

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        record = await service.get(task_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="task not found")
        return _to_json(record)

    @app.post("/tasks/{task_id}/cancel")
    async def cancel_task(task_id: str) -> dict:
        record = await service.cancel(task_id)
        if record is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND,
                                detail="task not found")
        return _to_json(record)

    return app
