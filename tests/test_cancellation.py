"""Cancellation: queued and running tasks; no success or partial results after."""

from __future__ import annotations

import asyncio

import pytest

from review_service.models import TaskState

from tests.conftest import BlockingPipeline

pytestmark = pytest.mark.asyncio


async def test_cancel_queued_task(make_service, repo_dir, monkeypatch):
    blocker = BlockingPipeline()
    monkeypatch.setattr("review_service.service.run_pipeline", blocker)
    svc = make_service(max_concurrency=1, max_queue=4)
    await svc.start()

    running, _ = await svc.submit("k-run", repo_dir)
    await asyncio.to_thread(blocker.entered.wait, 10)
    queued, _ = await svc.submit("k-queued", repo_dir)

    cancelled = await svc.cancel(queued.task_id)
    assert cancelled.state is TaskState.CANCELED

    blocker.release.set()
    await _wait_terminal(svc, running.task_id)
    await asyncio.sleep(0.1)  # give any stray work a chance to (wrongly) run
    final = await svc.get(queued.task_id)
    assert final.state is TaskState.CANCELED
    assert final.findings == []
    assert blocker.calls == [repo_dir]  # cancelled task never executed


async def test_cancel_running_task_leaves_no_result(make_service, repo_dir, monkeypatch):
    blocker = BlockingPipeline(findings=[{"rule": "no-todo", "path": "x", "line": 1,
                                          "message": "m"}])
    monkeypatch.setattr("review_service.service.run_pipeline", blocker)
    svc = make_service(max_concurrency=1, max_queue=0)
    await svc.start()

    rec, _ = await svc.submit("k-run", repo_dir)
    await asyncio.to_thread(blocker.entered.wait, 10)
    cancelled = await svc.cancel(rec.task_id)
    assert cancelled.state is TaskState.CANCELED

    blocker.release.set()  # pipeline now returns, but the result must be discarded
    await asyncio.sleep(0.2)
    final = await svc.get(rec.task_id)
    assert final.state is TaskState.CANCELED
    assert final.findings == []


async def test_cancel_terminal_task_is_noop(make_service, repo_dir):
    svc = make_service()
    await svc.start()
    rec, _ = await svc.submit("k-1", repo_dir)
    final = await _wait_terminal(svc, rec.task_id)
    assert final.state is TaskState.SUCCEEDED
    again = await svc.cancel(rec.task_id)
    assert again.state is TaskState.SUCCEEDED  # success result is preserved


async def test_cancel_unknown_task_returns_none(make_service):
    svc = make_service()
    await svc.start()
    assert await svc.cancel("no-such-task") is None


async def _wait_terminal(svc, task_id: str):
    for _ in range(50):
        await asyncio.sleep(0.05)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not reach a terminal state")
