"""HTTP API exposing the review service."""
from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from review_runner.errors import ErrorCategory, ReviewError
from review_runner.models import ReviewRequest
from review_runner.service import ReviewService

#: HTTP status per error category.
_STATUS_BY_CATEGORY = {
    ErrorCategory.VALIDATION_ERROR: 400,
    ErrorCategory.REPO_NOT_FOUND: 400,
    ErrorCategory.UNSUPPORTED_FILE: 422,
    ErrorCategory.MALFORMED_INPUT: 422,
    ErrorCategory.INDEX_ERROR: 422,
    ErrorCategory.RULE_EXECUTION_ERROR: 422,
    ErrorCategory.IDEMPOTENCY_CONFLICT: 409,
    ErrorCategory.QUEUE_FULL: 503,
    ErrorCategory.TASK_NOT_FOUND: 404,
    ErrorCategory.CANCELLED: 409,
    ErrorCategory.INTERRUPTED: 409,
    ErrorCategory.INTERNAL_ERROR: 500,
}


class SubmitBody(BaseModel):
    repo_path: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    rules: list[str] = Field(default_factory=list)


def _task_payload(task: Any) -> dict[str, Any]:
    return {
        "task_id": task.task_id,
        "idempotency_key": task.idempotency_key,
        "state": task.state.value,
        "request": task.request.__dict__,
        "result": task.result.to_dict() if task.result is not None else None,
        "error_category": task.error_category,
        "error_message": task.error_message,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "version": task.version,
    }


def create_app(service: ReviewService) -> FastAPI:
    app = FastAPI(
        title="Local AI Code Review Runner",
        version="0.1.0",
    )

    @app.exception_handler(ReviewError)
    async def _review_error_handler(_request, exc: ReviewError):
        status = _STATUS_BY_CATEGORY[exc.category]
        return JSONResponse(
            status_code=status,
            content={
                "error": {
                    "category": exc.category.value,
                    "message": exc.message,
                }
            },
        )

    @app.get("/health")
    def health():
        return {"status": "ok", "scheduler_running": service.scheduler.running}

    @app.post("/reviews", status_code=202)
    def submit_review(body: SubmitBody):
        request = ReviewRequest(
            repo_path=body.repo_path,
            idempotency_key=body.idempotency_key,
            rules=tuple(body.rules),
        )
        task, created = service.submit_request(request)
        return JSONResponse(
            status_code=201 if created else 200,
            content={"created": created, "task": _task_payload(task)},
        )

    @app.get("/reviews/by-key/{idempotency_key:path}")
    def get_review_by_key(idempotency_key: str):
        return _task_payload(service.get_by_idempotency_key(idempotency_key))

    @app.get("/reviews")
    def list_reviews():
        return {"tasks": [_task_payload(t) for t in service.list_tasks()]}

    @app.get("/reviews/{task_id}")
    def get_review(task_id: str):
        return _task_payload(service.get_task(task_id))

    @app.post("/reviews/{task_id}/cancel")
    def cancel_review(task_id: str):
        return _task_payload(service.cancel_task(task_id))

    return app
