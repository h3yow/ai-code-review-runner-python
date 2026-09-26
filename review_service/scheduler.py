"""Concurrency-limited asyncio scheduler with queueing and cancellation."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


class QueueFullError(Exception):
    """The wait queue is at capacity and the task was rejected outright."""

    def __init__(self, task_id: str, max_queue: int) -> None:
        self.task_id = task_id
        super().__init__(
            f"queue is full (max_queue={max_queue}); task {task_id} rejected"
        )


class Scheduler:
    """Runs coroutine factories with bounded parallelism and a bounded queue.

    All mutation happens on the event loop without intervening awaits, so
    enqueue/dequeue decisions are atomic with respect to other tasks.
    """

    def __init__(self, max_concurrency: int, max_queue: int) -> None:
        if max_concurrency < 1:
            raise ValueError("max_concurrency must be >= 1")
        if max_queue < 0:
            raise ValueError("max_queue must be >= 0")
        self._max_concurrency = max_concurrency
        self._max_queue = max_queue
        self._running: dict[str, asyncio.Task[None]] = {}
        self._queued: OrderedDict[str, Callable[[], Awaitable[None]]] = OrderedDict()

    # -- introspection ----------------------------------------------------

    def is_queued(self, task_id: str) -> bool:
        return task_id in self._queued

    def is_running(self, task_id: str) -> bool:
        return task_id in self._running

    @property
    def running_count(self) -> int:
        return len(self._running)

    @property
    def queued_count(self) -> int:
        return len(self._queued)

    # -- submission ---------------------------------------------------------

    async def submit(self, task_id: str, work: Callable[[], Awaitable[None]]) -> None:
        """Enqueue work; raises QueueFullError if the queue is saturated."""
        if task_id in self._running or task_id in self._queued:
            logger.info("task_id=%s already scheduled; ignoring duplicate submit", task_id)
            return
        if len(self._running) < self._max_concurrency:
            self._start(task_id, work)
        elif len(self._queued) < self._max_queue:
            self._queued[task_id] = work
            logger.info("task_id=%s queued (depth=%d)", task_id, len(self._queued))
        else:
            raise QueueFullError(task_id, self._max_queue)

    def _start(self, task_id: str, work: Callable[[], Awaitable[None]]) -> None:
        task = asyncio.ensure_future(work())
        self._running[task_id] = task
        task.add_done_callback(lambda _t, tid=task_id: self._on_done(tid))
        logger.info("task_id=%s started (running=%d)", task_id, len(self._running))

    def _on_done(self, task_id: str) -> None:
        self._running.pop(task_id, None)
        while self._queued and len(self._running) < self._max_concurrency:
            next_id, work = self._queued.popitem(last=False)
            self._start(next_id, work)

    # -- cancellation -------------------------------------------------------

    async def cancel(self, task_id: str) -> bool:
        """Cancel a queued or running task. Returns True if it was cancelled."""
        if task_id in self._queued:
            del self._queued[task_id]
            logger.info("task_id=%s cancelled while queued", task_id)
            return True
        task = self._running.get(task_id)
        if task is not None and not task.done():
            task.cancel()
            logger.info("task_id=%s cancellation requested while running", task_id)
            return True
        return False

    async def shutdown(self) -> None:
        queued = list(self._queued)
        self._queued.clear()
        for task_id in queued:
            logger.info("task_id=%s dropped from queue on shutdown", task_id)
        running = list(self._running.values())
        for task in running:
            task.cancel()
        if running:
            await asyncio.gather(*running, return_exceptions=True)
