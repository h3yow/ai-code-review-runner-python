"""Concurrency-limited task scheduler with queueing and cooperative cancel.

Concurrency: a fixed pool of worker threads executes at most
``max_concurrency`` tasks; extra submissions wait in a bounded queue and are
rejected with QUEUE_FULL once it is full.

Cancellation is cooperative: a per-task threading.Event is polled by the
pipeline between (and during) stages. A cancelled task always ends in
CANCELLED with no ReviewResult persisted -- partial findings never leak.

Commit-vs-cancel race: the decision "succeed or cancel?" and the storage
transition happen while holding the scheduler lock, which the cancel path
also holds while setting the cancel event. Exactly one outcome is committed.
"""
from __future__ import annotations

import queue
import threading

from review_runner.errors import ErrorCategory, ReviewError
from review_runner.logging_utils import get_logger
from review_runner.models import Task, TaskState
from review_runner.pipeline import CancellationRequested, ReviewPipeline
from review_runner.storage import TaskStore

logger = get_logger("review_runner.scheduler")

_SENTINEL = object()


class Scheduler:
    def __init__(
        self,
        store: TaskStore,
        pipeline: ReviewPipeline,
        max_concurrency: int = 2,
        max_queue_size: int = 100,
    ) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if max_queue_size < 0:
            raise ValueError("max_queue_size must be >= 0")
        self.store = store
        self.pipeline = pipeline
        self.max_concurrency = max_concurrency
        self.max_queue_size = max_queue_size
        self._cancel_events: dict[str, threading.Event] = {}
        self._workers: list[threading.Thread] = []
        # Guards _cancel_events AND the atomic "check event + commit transition"
        # critical sections. Reentrant: helpers called while already holding
        # the lock (e.g. is_cancelled) must not self-deadlock. The store has
        # its own lock; nesting order is always scheduler -> store, never
        # the reverse.
        self._lock = threading.RLock()
        self._started = False
        self._stopping = threading.Event()
        # NOTE: queue.Queue(maxsize=0) means *unbounded* in Python, so the
        # waiting-room capacity is enforced with an explicit counter instead.
        self._queue: "queue.Queue[object]" = queue.Queue()
        # Tasks admitted but not yet finished by a worker (queued + running).
        self._active = 0

    @property
    def capacity(self) -> int:
        """At most this many tasks may be queued+running simultaneously."""
        return self.max_concurrency + self.max_queue_size

    # ---- lifecycle ----------------------------------------------------
    def start(self) -> None:
        with self._lock:
            if self._started:
                return
            self._started = True
            self._stopping.clear()
            self._workers = []
            for i in range(self.max_concurrency):
                t = threading.Thread(
                    target=self._worker_loop,
                    name=f"review-worker-{i}",
                    daemon=True,
                )
                t.start()
                self._workers.append(t)
        logger.info(
            "SCHEDULER started max_concurrency=%d max_queue_size=%d",
            self.max_concurrency,
            self.max_queue_size,
        )

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            if not self._started:
                return
            self._stopping.set()
            workers = list(self._workers)
        for _ in workers:
            self._queue.put(_SENTINEL)
        for t in workers:
            t.join(timeout=timeout)
        with self._lock:
            self._started = False
            self._workers = []
        logger.info("SCHEDULER stopped")

    @property
    def running(self) -> bool:
        return self._started and not self._stopping.is_set()

    # ---- submission / cancel -----------------------------------------
    def submit(self, task: Task) -> None:
        """Enqueue a PENDING task; raises QUEUE_FULL when capacity is full."""
        with self._lock:
            if self._active >= self.capacity:
                raise ReviewError(
                    ErrorCategory.QUEUE_FULL,
                    "queue is full; retry later or increase the capacity",
                )
            self._cancel_events[task.task_id] = threading.Event()
            self._active += 1
        self._queue.put(task.task_id)
        logger.info(
            "TASK task_id=%s idempotency_key=%s note=enqueued active=%d/%d",
            task.task_id,
            task.idempotency_key,
            self._active,
            self.capacity,
        )

    def enqueue_unchecked(self, task_id: str) -> bool:
        """Enqueue without QUEUE_FULL rejection (startup recovery only).

        Blocks until capacity frees up. Returns False when the task is no
        longer enqueueable (terminal / missing).
        """
        task = self.store.get(task_id)
        if task is None or task.state is not TaskState.PENDING:
            return False
        with self._lock:
            self._cancel_events.setdefault(task_id, threading.Event())
            self._active += 1
        self._queue.put(task_id)
        logger.info("TASK task_id=%s note=reenqueued-on-startup", task_id)
        return True

    def cancel(self, task_id: str) -> Task:
        """Request cancellation for a queued or running task.

        PENDING   -> CANCELLING -> CANCELLED (committed immediately, the
                     queued worker stands down on dequeue).
        RUNNING   -> CANCELLING (cancel event set; the in-flight pipeline
                     aborts at its next checkpoint and commits CANCELLED).
        CANCELLING-> idempotent no-op (event re-armed).
        Terminal  -> returned unchanged.
        """
        with self._lock:
            task = self.store.get(task_id)
            if task is None:
                raise ReviewError(
                    ErrorCategory.TASK_NOT_FOUND, f"unknown task: {task_id}"
                )

            for _ in range(5):
                if task.state in (
                    TaskState.SUCCEEDED,
                    TaskState.FAILED,
                    TaskState.CANCELLED,
                ):
                    return task

                event = self._cancel_events.get(task_id)
                if event is None:
                    event = threading.Event()
                    self._cancel_events[task_id] = event
                event.set()

                if task.state is TaskState.CANCELLING:
                    return task

                try:
                    task = self.store.transition(
                        task_id, task.version, TaskState.CANCELLING
                    )
                except RuntimeError:
                    # Worker committed concurrently; reload and re-evaluate.
                    latest = self.store.get(task_id)
                    if latest is None:
                        raise ReviewError(
                            ErrorCategory.TASK_NOT_FOUND,
                            f"unknown task: {task_id}",
                        )
                    task = latest
                    continue

                if task.state is TaskState.CANCELLING:
                    # From PENDING there is no worker allowed to advance it;
                    # finalize immediately. From RUNNING the pipeline aborts
                    # and performs CANCELLING -> CANCELLED itself.
                    # Distinguish via queue presence is unnecessary: a task
                    # dequeued by a worker is RUNNING before we run. A task we
                    # moved from PENDING may have already been dequeued, in
                    # which case the worker saw CANCELLING and stood down --
                    # so always finalize CANCELLING -> CANCELLED here; if the
                    # pipeline is still executing it will observe state
                    # CANCELLED on commit and stand down too.
                    try:
                        task = self.store.transition(
                            task_id,
                            task.version,
                            TaskState.CANCELLED,
                            error_category=ErrorCategory.CANCELLED.value,
                            error_message=(
                                "task cancelled; partial results discarded"
                            ),
                        )
                    except RuntimeError:
                        latest = self.store.get(task_id)
                        if latest is not None:
                            task = latest
                    return task
            return task

    def is_cancelled(self, task_id: str) -> bool:
        with self._lock:
            event = self._cancel_events.get(task_id)
            return event is not None and event.is_set()

    # ---- worker -------------------------------------------------------
    def _worker_loop(self) -> None:
        while not self._stopping.is_set():
            item = self._queue.get()
            try:
                if item is _SENTINEL:
                    return
                if self._stopping.is_set():
                    # Shutting down: leave the task PENDING. It is still
                    # persisted and will be re-enqueued by startup recovery.
                    continue
                self._execute(item)  # type: ignore[arg-type]
            finally:
                self._release_slot(item)
                self._queue.task_done()

    def _release_slot(self, item: object) -> None:
        """Release one admission slot after a task left the queue."""
        if item is _SENTINEL:
            return
        with self._lock:
            self._active = max(0, self._active - 1)

    def _execute(self, task_id: str) -> None:
        task = self.store.get(task_id)
        if task is None:
            self._drop_event(task_id)
            return
        if task.state is not TaskState.PENDING:
            # Already cancelled/finalized while queued: stand down.
            self._drop_event(task_id)
            return

        try:
            task = self.store.transition(task_id, task.version, TaskState.RUNNING)
        except RuntimeError:
            return  # PENDING was superseded (e.g. cancellation)

        try:
            result = self.pipeline.run(
                task.request, lambda: self.is_cancelled(task_id)
            )
        except CancellationRequested:
            self._finalize_cancelled(task_id)
            return
        except ReviewError as exc:
            self._finalize_failed(task_id, exc.category, exc.message)
            return
        except Exception as exc:  # defensive: categorized, never silent
            self._finalize_failed(
                task_id, ErrorCategory.INTERNAL_ERROR, f"unexpected error: {exc}"
            )
            return

        # Decide-and-commit atomically vs. the cancel path.
        with self._lock:
            if self.is_cancelled(task_id):
                self._commit_cancelled_locked(task_id)
            else:
                self._commit_success_locked(task_id, result)
        self._drop_event(task_id)

    # ---- finalization helpers ----------------------------------------
    def _commit_success_locked(self, task_id: str, result: object) -> None:
        """Caller holds self._lock."""
        for _ in range(5):
            task = self.store.get(task_id)
            if task is None or task.state is not TaskState.RUNNING:
                return
            try:
                self.store.transition(
                    task_id, task.version, TaskState.SUCCEEDED, result=result
                )
                return
            except RuntimeError:
                continue

    def _commit_cancelled_locked(self, task_id: str) -> None:
        """Caller holds self._lock."""
        for _ in range(5):
            task = self.store.get(task_id)
            if task is None:
                return
            if task.state is TaskState.CANCELLED:
                return
            if task.state is TaskState.SUCCEEDED or task.state is TaskState.FAILED:
                return
            try:
                if task.state is TaskState.RUNNING:
                    task = self.store.transition(
                        task_id, task.version, TaskState.CANCELLING
                    )
                    continue
                self.store.transition(
                    task_id,
                    task.version,
                    TaskState.CANCELLED,
                    error_category=ErrorCategory.CANCELLED.value,
                    error_message="task cancelled; partial results discarded",
                )
                return
            except RuntimeError:
                continue

    def _finalize_cancelled(self, task_id: str) -> None:
        with self._lock:
            self._commit_cancelled_locked(task_id)
        self._drop_event(task_id)

    def _finalize_failed(
        self, task_id: str, category: ErrorCategory, message: str
    ) -> None:
        # Cancellation always wins over a concurrently observed failure.
        with self._lock:
            if self.is_cancelled(task_id):
                self._commit_cancelled_locked(task_id)
                self._drop_event(task_id)
                return
            for _ in range(5):
                task = self.store.get(task_id)
                if task is None:
                    return
                if task.state in (TaskState.CANCELLING,):
                    self._commit_cancelled_locked(task_id)
                    return
                if task.state is not TaskState.RUNNING:
                    return
                try:
                    self.store.transition(
                        task_id,
                        task.version,
                        TaskState.FAILED,
                        error_category=category.value,
                        error_message=message,
                    )
                    return
                except RuntimeError:
                    continue
        self._drop_event(task_id)

    def _drop_event(self, task_id: str) -> None:
        with self._lock:
            self._cancel_events.pop(task_id, None)
