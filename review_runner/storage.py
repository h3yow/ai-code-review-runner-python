"""Persistent task store backed by a local JSON file.

Every mutation is performed under an in-process lock, validated against the
state machine and atomically persisted (temp file + os.replace), so a crash at
any point leaves either the old or the new state on disk -- never a partial
record.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from pathlib import Path

from review_runner.errors import ErrorCategory
from review_runner.logging_utils import get_logger, log_transition
from review_runner.models import (
    TERMINAL_STATES,
    Task,
    TaskState,
    validate_transition,
)

logger = get_logger("review_runner.storage")

# Fields callers are allowed to change together with a state transition.
_MUTABLE_FIELDS = frozenset({"result", "error_category", "error_message"})


class TaskStore:
    """File-backed task store. All public operations are serialized by a lock."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self._tasks: dict[str, Task] = {}
        self._idempotency_index: dict[str, str] = {}

    # ---- persistence --------------------------------------------------
    def load(self) -> None:
        with self._lock:
            self._tasks.clear()
            self._idempotency_index.clear()
            if not self.path.exists():
                return
            with self.path.open("r", encoding="utf-8") as fh:
                data = json.load(fh)
            for record in data.get("tasks", []):
                task = Task.from_record(record)
                self._tasks[task.task_id] = task
                self._idempotency_index[task.idempotency_key] = task.task_id

    def _persist_locked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "version": 1,
            "tasks": [t.to_record() for t in self._tasks.values()],
        }
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_name, self.path)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass
            raise

    # ---- reads --------------------------------------------------------
    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def get_by_idempotency_key(self, key: str) -> Task | None:
        with self._lock:
            task_id = self._idempotency_index.get(key)
            return self._tasks.get(task_id) if task_id is not None else None

    def all_tasks(self) -> list[Task]:
        with self._lock:
            return sorted(self._tasks.values(), key=lambda t: t.created_at)

    # ---- writes -------------------------------------------------------
    def insert_if_absent(self, task: Task) -> Task:
        """Return the stored task (the existing one or the inserted one)."""
        with self._lock:
            existing = self._tasks.get(task.task_id)
            if existing is not None:
                return existing
            by_key = self._idempotency_index.get(task.idempotency_key)
            if by_key is not None:
                return self._tasks[by_key]
            self._tasks[task.task_id] = task
            self._idempotency_index[task.idempotency_key] = task.task_id
            self._persist_locked()
            logger.info(
                "TASK task_id=%s idempotency_key=%s transition=-->%s note=created",
                task.task_id,
                task.idempotency_key,
                task.state.value,
            )
            return task

    def transition(
        self,
        task_id: str,
        expected_version: int,
        target: TaskState,
        **changes: object,
    ) -> Task:
        """CAS-style transition.

        Raises RuntimeError on a stale version or an illegal state jump.
        Allowed change fields: result, error_category, error_message.
        """
        extra = set(changes) - _MUTABLE_FIELDS
        if extra:
            raise RuntimeError(f"unsupported change fields: {sorted(extra)}")
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                raise RuntimeError(f"unknown task: {task_id}")
            if task.version != expected_version:
                raise RuntimeError(
                    f"stale version for {task_id}: "
                    f"expected {expected_version}, current {task.version}"
                )
            from_state = task.state
            validate_transition(from_state, target)
            if "result" in changes:
                task.result = changes["result"]  # type: ignore[assignment]
            if "error_category" in changes:
                task.error_category = changes["error_category"]  # type: ignore[assignment]
            if "error_message" in changes:
                task.error_message = changes["error_message"]  # type: ignore[assignment]
            task.state = target
            task.version += 1
            task.updated_at = time.time()
            self._persist_locked()
            log_transition(
                logger,
                task.task_id,
                task.idempotency_key,
                from_state.value,
                target.value,
                error_category=task.error_category,
            )
            return task

    # ---- recovery -----------------------------------------------------
    def recover_on_startup(self) -> list[Task]:
        """Converge non-terminal tasks after a restart.

        - PENDING tasks stay PENDING and are returned for re-enqueueing.
        - RUNNING/CANCELLING tasks are FAILED with category INTERRUPTED:
          their partial conclusions are discarded, so results are never
          double-counted from a pre-crash run.
        """
        requeue: list[Task] = []
        with self._lock:
            for task in list(self._tasks.values()):
                if task.state in TERMINAL_STATES:
                    continue
                if task.state is TaskState.PENDING:
                    requeue.append(task)
                    continue
                # RUNNING or CANCELLING when the process died.
                from_state = task.state
                task.state = TaskState.FAILED
                task.error_category = ErrorCategory.INTERRUPTED.value
                task.error_message = (
                    "task was interrupted by a process restart; partial results discarded"
                )
                task.version += 1
                task.updated_at = time.time()
                log_transition(
                    logger,
                    task.task_id,
                    task.idempotency_key,
                    from_state.value,
                    TaskState.FAILED.value,
                    error_category=ErrorCategory.INTERRUPTED.value,
                    note="startup-recovery",
                )
            if any(
                t.state is TaskState.FAILED
                and t.error_category == ErrorCategory.INTERRUPTED.value
                for t in self._tasks.values()
            ):
                self._persist_locked()
        return requeue
