"""ReviewService: orchestrates submission, lifecycle, cancellation, recovery."""

from __future__ import annotations

import asyncio
import logging
import uuid

from review_service.models import (
    CapacityConfig,
    ErrorCategory,
    InvalidTransitionError,
    ReviewRequest,
    ScopeConfig,
    TaskRecord,
    TaskState,
    TERMINAL_STATES,
)
from review_service.pipeline import StageError, run_pipeline
from review_service.scheduler import QueueFullError, Scheduler
from review_service.store import TaskStore

logger = logging.getLogger(__name__)


class ReviewService:
    def __init__(
        self,
        store: TaskStore,
        scheduler: Scheduler,
        default_capacity: CapacityConfig | None = None,
    ) -> None:
        self._store = store
        self._scheduler = scheduler
        self._default_capacity = default_capacity or CapacityConfig()

    # -- lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        """Recover in-flight tasks from a previous run and requeue them."""
        recovered = self._store.reset_inflight()
        for record in recovered:
            logger.info(
                "task_id=%s idempotency_key=%s recovered after restart; requeueing",
                record.task_id, record.idempotency_key,
            )
            await self._scheduler.submit(record.task_id, self._make_work(record.task_id))

    async def shutdown(self) -> None:
        await self._scheduler.shutdown()
        self._store.close()

    # -- submission -----------------------------------------------------------

    async def submit(
        self,
        idempotency_key: str,
        repo_path: str,
        rules: tuple[str, ...] = (),
        scope: ScopeConfig | None = None,
        capacity: CapacityConfig | None = None,
    ) -> tuple[TaskRecord, bool]:
        """Submit a review task. Returns (record, created_new).

        Replaying an existing idempotency key with the same payload returns
        the original record with created_new=False. A conflicting payload
        raises IdempotencyConflictError.
        """
        request = ReviewRequest(
            repo_path=repo_path,
            rules=rules,
            scope=scope or ScopeConfig(),
            capacity=capacity or self._default_capacity,
        )
        record = TaskRecord(
            task_id=uuid.uuid4().hex,
            idempotency_key=idempotency_key,
            request=request,
        )
        record = self._store.insert_pending(record)
        if record.state is not TaskState.PENDING:
            logger.info(
                "task_id=%s idempotency_key=%s duplicate submission; returning existing",
                record.task_id, record.idempotency_key,
            )
            return record, False
        logger.info(
            "task_id=%s idempotency_key=%s accepted repo_path=%s rules=%s",
            record.task_id, record.idempotency_key, repo_path, sorted(rules),
        )
        self._store.transition(record.task_id, TaskState.QUEUED)
        try:
            await self._scheduler.submit(record.task_id, self._make_work(record.task_id))
        except QueueFullError as exc:
            self._store.transition(
                record.task_id, TaskState.FAILED,
                error_category=ErrorCategory.CAPACITY, error_detail=str(exc),
            )
            raise
        return record, True

    # -- queries --------------------------------------------------------------

    async def get(self, task_id: str) -> TaskRecord | None:
        return self._store.get(task_id)

    async def list_tasks(self) -> list[TaskRecord]:
        return self._store.list_all()

    # -- cancellation -----------------------------------------------------------

    async def cancel(self, task_id: str) -> TaskRecord | None:
        record = self._store.get(task_id)
        if record is None:
            return None
        if record.state in TERMINAL_STATES:
            return record
        await self._scheduler.cancel(task_id)
        try:
            return self._store.transition(task_id, TaskState.CANCELED)
        except InvalidTransitionError:
            # The task reached a terminal state concurrently; that result wins.
            return self._store.get(task_id)

    # -- execution wrapper ------------------------------------------------------

    def _make_work(self, task_id: str):
        async def work() -> None:
            self._store.transition(task_id, TaskState.RUNNING)
            record = self._store.get(task_id)
            assert record is not None
            try:
                result = await asyncio.to_thread(
                    run_pipeline,
                    record.request.repo_path,
                    record.request.rules,
                    record.request.scope,
                    record.request.capacity,
                )
            except asyncio.CancelledError:
                # State is written by cancel(); a cancelled task must not
                # produce a success result or partial findings.
                logger.info("task_id=%s execution cancelled", task_id)
                raise
            except StageError as exc:
                self._transition_terminal(
                    task_id, TaskState.FAILED,
                    error_category=exc.category, error_detail=exc.detail,
                )
                return
            except Exception as exc:  # noqa: BLE001 - categorize everything
                self._transition_terminal(
                    task_id, TaskState.FAILED,
                    error_category=ErrorCategory.INTERNAL, error_detail=str(exc),
                )
                return
            summary = result.summary
            logger.info(
                "task_id=%s reviewed=%d excluded=%d failed=%d skipped=%d truncated=%s",
                task_id, summary.reviewed, summary.excluded, summary.failed,
                summary.skipped, summary.truncated,
            )
            self._transition_terminal(
                task_id, TaskState.SUCCEEDED,
                findings=result.findings,
                file_results=result.file_results,
                summary=result.summary,
            )
        return work

    def _transition_terminal(self, task_id: str, target: TaskState, **fields: object) -> None:
        try:
            self._store.transition(task_id, target, **fields)
        except InvalidTransitionError:
            current = self._store.get(task_id)
            logger.info(
                "task_id=%s result %s discarded; task already %s",
                task_id, target.value,
                current.state.value if current else "missing",
            )
