"""Shared test fixtures and helpers."""
from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from review_runner.logging_utils import configure_logging
from review_runner.models import Task
from review_runner.service import ReviewService
from tests.util import wait_for_state  # noqa: F401

configure_logging(logging.INFO)


@pytest.fixture
def tmp_repo(tmp_path: Path) -> Path:
    """A tiny valid local repository to review."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "hello.py").write_text(
        "# TODO: clean up\nprint('hello')\n", encoding="utf-8"
    )
    (repo / "README.md").write_text("# demo\nshort line\n", encoding="utf-8")
    return repo


@pytest.fixture
def make_service(tmp_path: Path) -> Iterator[Callable[..., ReviewService]]:
    services: list[ReviewService] = []

    def _factory(
        *,
        repo_pipeline=None,
        max_concurrency: int = 2,
        max_queue_size: int = 100,
        name: str = "tasks.json",
    ) -> ReviewService:
        svc = ReviewService(
            store_path=tmp_path / name,
            max_concurrency=max_concurrency,
            max_queue_size=max_queue_size,
            pipeline=repo_pipeline,
            auto_start=False,
        )
        services.append(svc)
        return svc

    yield _factory

    for svc in services:
        svc.shutdown()

