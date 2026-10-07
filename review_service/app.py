"""FastAPI HTTP layer exposing the review service."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException, Response, status
from pydantic import BaseModel, Field

from review_service.models import ScopeConfig, TaskRecord
from review_service.scheduler import QueueFullError
from review_service.service import ReviewService
from review_service.store import IdempotencyConflictError


class ScopePayload(BaseModel):
    """User-configurable review scope; all fields optional."""

    include_dirs: list[str] = []
    include_names: list[str] = []
    include_extensions: list[str] | None = None  # None -> default (".py",)
    exclude: list[str] = []
    recursive: bool = True

    def to_config(self) -> ScopeConfig:
        return ScopeConfig.from_dict(self.model_dump())


class SubmitRequest(BaseModel):
    idempotency_key: str = Field(min_length=1)
    repo_path: str = Field(min_length=1)
    rules: list[str] = []
    scope: ScopePayload = ScopePayload()


def _to_json(record: TaskRecord) -> dict:
    return {
        "task_id": record.task_id,
        "idempotency_key": record.idempotency_key,
        "repo_path": record.request.repo_path,
        "rules": sorted(record.request.rules),
        "scope": record.request.scope.to_dict(),
        "state": record.state.value,
        "error_category": record.error_category.value if record.error_category else None,
        "error_detail": record.error_detail,
        "findings": record.findings,
        "files": record.file_results,
        "summary": record.summary,
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }


def create_app(service: ReviewService) -> FastAPI:
    app = FastAPI(title="ai-code-review-runner")

    @app.post("/tasks", status_code=status.HTTP_201_CREATED)
    async def submit_task(req: SubmitRequest, response: Response) -> dict:
        try:
            record, created = await service.submit(
                req.idempotency_key, req.repo_path, tuple(req.rules),
                req.scope.to_config(),
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
