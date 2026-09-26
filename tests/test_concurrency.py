"""Concurrency limits: bounded parallelism, queueing, explicit rejection."""

from __future__ import annotations

import asyncio

import pytest

from review_service.models import ErrorCategory, TaskState
from review_service.scheduler import QueueFullError

from tests.conftest import BlockingPipeline

pytestmark = pytest.mark.asyncio


async def test_parallel_limit_respected(make_service, repo_dir, monkeypatch):
    blocker = BlockingPipeline()
    monkeypatch.setattr("review_service.service.run_pipeline", blocker)
    svc = make_service(max_concurrency=2, max_queue=8)
    await svc.start()

    records = [await svc.submit(f"k-{i}", repo_dir) for i in range(4)]
    assert all(created for _, created in records)
    await asyncio.to_thread(blocker.entered.wait, 10)
    await asyncio.sleep(0.05)
    assert svc._scheduler.running_count == 2  # bounded by max_concurrency
    assert svc._scheduler.queued_count == 2   # the rest wait in queue

    blocker.release.set()
    for rec, _ in records:
        final = await _wait_terminal(svc, rec.task_id)
        assert final.state is TaskState.SUCCEEDED
    assert svc._scheduler.running_count == 0


async def test_queue_full_rejected_with_distinguishable_reason(
    make_service, repo_dir, monkeypatch
):
    blocker = BlockingPipeline()
    monkeypatch.setattr("review_service.service.run_pipeline", blocker)
    svc = make_service(max_concurrency=1, max_queue=1)
    await svc.start()

    await svc.submit("k-run", repo_dir)          # occupies the single slot
    await asyncio.to_thread(blocker.entered.wait, 10)
    await svc.submit("k-queued", repo_dir)       # fills the queue
    with pytest.raises(QueueFullError):
        await svc.submit("k-overflow", repo_dir)  # explicitly rejected

    rejected = await svc.get((await svc.list_tasks())[2].task_id)
    assert rejected.state is TaskState.FAILED
    assert rejected.error_category is ErrorCategory.CAPACITY

    blocker.release.set()
    for rec in (await svc.list_tasks())[:2]:
        final = await _wait_terminal(svc, rec.task_id)
        assert final.state is TaskState.SUCCEEDED


async def _wait_terminal(svc, task_id: str):
    for _ in range(50):
        await asyncio.sleep(0.05)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not reach a terminal state")
