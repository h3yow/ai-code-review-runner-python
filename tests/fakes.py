"""Deterministic, in-memory pipeline used to drive concurrency/cancel tests.

The pipeline blocks at a gate until the test releases it, polling the
cooperative cancellation flag, so tests can precisely interleave queueing,
running and cancellation without sleeping races.
"""
from __future__ import annotations

import threading
from collections.abc import Callable

from review_runner.models import ReviewRequest, ReviewResult
from review_runner.pipeline import CancellationRequested


class GatedPipeline:
    def __init__(self) -> None:
        self.gate = threading.Event()
        self.started = threading.Event()
        self.started_ids: list[str] = []
        self._lock = threading.Lock()

    def run(
        self,
        request: ReviewRequest,
        is_cancelled: Callable[[], bool],
    ) -> ReviewResult:
        with self._lock:
            self.started_ids.append(request.idempotency_key)
            if len(self.started_ids) == 1:
                self.started.set()
        # Block at the gate while remaining cancellation-responsive.
        while not self.gate.is_set():
            if is_cancelled():
                raise CancellationRequested()
            self.gate.wait(timeout=0.01)
        if is_cancelled():
            raise CancellationRequested()
        return ReviewResult(findings=[], files_scanned=0)


class FailingGatedPipeline(GatedPipeline):
    """Succeeds except for one idempotency key, which fails at rule stage."""

    def __init__(self, failing_key: str, category) -> None:
        super().__init__()
        self.failing_key = failing_key
        self.category = category

    def run(self, request, is_cancelled):
        from review_runner.errors import ReviewError

        with self._lock:
            self.started_ids.append(request.idempotency_key)
            if len(self.started_ids) == 1:
                self.started.set()
        while not self.gate.is_set():
            if is_cancelled():
                raise CancellationRequested()
            self.gate.wait(timeout=0.01)
        if request.idempotency_key == self.failing_key:
            raise ReviewError(self.category, "injected failure")
        return ReviewResult(findings=[], files_scanned=0)
