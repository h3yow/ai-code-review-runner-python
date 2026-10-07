"""Entrypoint: run the review service locally (no external dependencies)."""

from __future__ import annotations

import asyncio
import os

import uvicorn

from review_service.app import create_app
from review_service.models import LimitPolicy, ReviewLimits
from review_service.scheduler import Scheduler
from review_service.service import ReviewService
from review_service.store import TaskStore

DB_PATH = os.environ.get("REVIEW_DB_PATH", "review_tasks.db")
MAX_CONCURRENCY = int(os.environ.get("REVIEW_MAX_CONCURRENCY", "2"))
MAX_QUEUE = int(os.environ.get("REVIEW_MAX_QUEUE", "16"))
LIMITS = ReviewLimits(
    max_files=int(os.environ.get("REVIEW_MAX_FILES", "5000")),
    max_total_bytes=int(os.environ.get("REVIEW_MAX_TOTAL_BYTES", str(50 * 1024 * 1024))),
    policy=LimitPolicy(os.environ.get("REVIEW_LIMIT_POLICY", "REJECT")),
)

service = ReviewService(TaskStore(DB_PATH), Scheduler(MAX_CONCURRENCY, MAX_QUEUE),
                        limits=LIMITS)
app = create_app(service)


@app.on_event("startup")
async def _startup() -> None:
    await service.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await service.shutdown()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
