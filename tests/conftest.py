"""Shared fixtures. Logging is configured at INFO so test output shows
task ids, idempotency keys, state transitions and error categories."""

from __future__ import annotations

import logging
import os
import threading

import pytest

from review_service.scheduler import Scheduler
from review_service.service import ReviewService
from review_service.store import TaskStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


@pytest.fixture()
def repo_dir(tmp_path):
    """A local repository containing one reviewable Python file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "ok.py").write_text("def f():\n    return 1  # TODO fix\n")
    return str(repo)


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "tasks.db")


@pytest.fixture()
def make_service(db_path):
    """Factory for services; all created services are shut down after the test."""
    created: list[ReviewService] = []

    def factory(max_concurrency: int = 2, max_queue: int = 8,
                path: str | None = None, limits=None) -> ReviewService:
        svc = ReviewService(TaskStore(path or db_path),
                            Scheduler(max_concurrency, max_queue),
                            limits=limits)
        created.append(svc)
        return svc

    yield factory
    for svc in created:
        try:
            import asyncio
            asyncio.get_event_loop().run_until_complete(svc.shutdown())
        except Exception:
            pass


class BlockingPipeline:
    """Pipeline stand-in that blocks until released, for concurrency tests."""

    def __init__(self, findings: list[dict] | None = None) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()
        self.calls: list[str] = []
        self._findings = findings or []

    def __call__(self, repo_path: str, rules: tuple[str, ...], *args) -> object:
        from review_service.pipeline import PipelineResult

        self.calls.append(repo_path)
        self.entered.set()
        self.release.wait(timeout=10)
        return PipelineResult(
            findings=list(self._findings),
            files=[],
            summary={"reviewed": 0, "excluded": 0, "failed": 0,
                     "truncated": False, "has_skips_or_failures": False},
        )
