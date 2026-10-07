"""Entrypoint: run the review service locally (no external dependencies)."""

from __future__ import annotations

import asyncio
import os

import uvicorn

from review_service.app import create_app
from review_service.models import CapacityConfig, OverflowPolicy
from review_service.scheduler import Scheduler
from review_service.service import ReviewService
from review_service.store import TaskStore

DB_PATH = os.environ.get("REVIEW_DB_PATH", "review_tasks.db")
MAX_CONCURRENCY = int(os.environ.get("REVIEW_MAX_CONCURRENCY", "2"))
MAX_QUEUE = int(os.environ.get("REVIEW_MAX_QUEUE", "16"))
DEFAULT_MAX_FILES = int(os.environ.get("REVIEW_MAX_FILES", "10000"))
DEFAULT_MAX_TOTAL_BYTES = int(
    os.environ.get("REVIEW_MAX_TOTAL_BYTES", str(10 * 1024 * 1024))
)
DEFAULT_OVERFLOW_POLICY = OverflowPolicy(
    os.environ.get("REVIEW_OVERFLOW_POLICY", "reject")
)

default_capacity = CapacityConfig(
    max_files=DEFAULT_MAX_FILES,
    max_total_bytes=DEFAULT_MAX_TOTAL_BYTES,
    overflow_policy=DEFAULT_OVERFLOW_POLICY,
)
service = ReviewService(
    TaskStore(DB_PATH),
    Scheduler(MAX_CONCURRENCY, MAX_QUEUE),
    default_capacity=default_capacity,
)
app = create_app(service)


@app.on_event("startup")
async def _startup() -> None:
    await service.start()


@app.on_event("shutdown")
async def _shutdown() -> None:
    await service.shutdown()


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8000)
