"""Application service: submission, lifecycle queries, cancellation.

Owns the store, pipeline and scheduler and applies the idempotency rules:

* same idempotency key + same content -> the single existing task is
  returned (created=False), no second result is ever produced;
* same idempotency key + different content -> IDEMPOTENCY_CONFLICT;
* queue full -> the submission is persisted as FAILED/QUEUE_FULL so the
  rejection itself is the single deterministic result for that key.
"""
from __future__ import annotations

import threading
from pathlib import Path

from review_runner.errors import ErrorCategory, ReviewError
from review_runner.logging_utils import get_logger
from review_runner.models import ReviewRequest, Task, TaskState
from review_runner.pipeline import ReviewPipeline
from review_runner.scheduler import Scheduler
from review_runner.storage import TaskStore

logger = get_logger("review_runner.service")


class ReviewService:
    def __init__(
        self,
        store_path: str | Path = "tasks.json",
        max_concurrency: int = 2,
        max_queue_size: int = 100,
        pipeline: ReviewPipeline | None = None,
        auto_start: bool = True,
    ) -> None:
        self.store = TaskStore(store_path)
        self.pipeline = pipeline or ReviewPipeline()
        self.scheduler = Scheduler(
            self.store,
            self.pipeline,
            max_concurrency=max_concurrency,
            max_queue_size=max_queue_size,
        )
        self._submit_lock = threading.Lock()
        self._started = False
        self.store.load()
        if auto_start:
            self.start()

    # ---- lifecycle ----------------------------------------------------
    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self.scheduler.start()
        requeue = self.store.recover_on_startup()
        for task in requeue:
            self.scheduler.enqueue_unchecked(task.task_id)
        logger.info(
            "SERVICE started requeued_pending=%d", len(requeue)
        )

    def shutdown(self) -> None:
        if not self._started:
            return
        self.scheduler.stop()
        self._started = False
        logger.info("SERVICE stopped")

    # ---- operations ---------------------------------------------------
    def submit_request(self, request: ReviewRequest) -> tuple[Task, bool]:
        """Return ``(task, created)``.

        Repeats (same key, same fingerprint) return the existing task with
        ``created=False``. Same key with a different fingerprint raises
        IDEMPOTENCY_CONFLICT.
        """
        with self._submit_lock:
            existing = self.store.get_by_idempotency_key(request.idempotency_key)
            if existing is not None:
                if existing.request_fingerprint != request.fingerprint():
                    raise ReviewError(
                        ErrorCategory.IDEMPOTENCY_CONFLICT,
                        (
                            f"idempotency_key={request.idempotency_key!r} was already "
                            f"used with different request content "
                            f"(existing task {existing.task_id})"
                        ),
                    )
                logger.info(
                    "TASK task_id=%s idempotency_key=%s note=duplicate-submission-returned",
                    existing.task_id,
                    existing.idempotency_key,
                )
                return existing, False

            task = Task.create(request)
            stored = self.store.insert_if_absent(task)
            try:
                self.scheduler.submit(stored)
            except ReviewError as exc:
                if exc.category is ErrorCategory.QUEUE_FULL:
                    latest = self.store.get(stored.task_id)
                    if latest is not None and latest.state is TaskState.PENDING:
                        self.store.transition(
                            stored.task_id,
                            latest.version,
                            TaskState.FAILED,
                            error_category=ErrorCategory.QUEUE_FULL.value,
                            error_message=exc.message,
                        )
                raise
            return stored, True

    def get_task(self, task_id: str) -> Task:
        task = self.store.get(task_id)
        if task is None:
            raise ReviewError(
                ErrorCategory.TASK_NOT_FOUND, f"unknown task_id: {task_id}"
            )
        return task

    def get_by_idempotency_key(self, key: str) -> Task:
        task = self.store.get_by_idempotency_key(key)
        if task is None:
            raise ReviewError(
                ErrorCategory.TASK_NOT_FOUND,
                f"no task for idempotency_key: {key}",
            )
        return task

    def cancel_task(self, task_id: str) -> Task:
        return self.scheduler.cancel(task_id)

    def list_tasks(self) -> list[Task]:
        return self.store.all_tasks()
