"""Idempotent submission: one key -> one result; conflicts are distinguishable."""

from __future__ import annotations

import asyncio
import threading

import pytest

from review_service.models import ReviewRequest, TaskRecord, TaskState
from review_service.store import IdempotencyConflictError, TaskStore

pytestmark = pytest.mark.asyncio


async def test_duplicate_submission_returns_same_task(make_service, repo_dir):
    svc = make_service()
    await svc.start()
    first, created1 = await svc.submit("key-1", repo_dir)
    second, created2 = await svc.submit("key-1", repo_dir)
    assert created1 is True
    assert created2 is False
    assert second.task_id == first.task_id


async def test_conflicting_payload_rejected(make_service, repo_dir):
    svc = make_service()
    await svc.start()
    await svc.submit("key-1", repo_dir)
    with pytest.raises(IdempotencyConflictError) as excinfo:
        await svc.submit("key-1", repo_dir, ("no-todo",))
    err = excinfo.value
    assert err.key == "key-1"
    assert err.existing_task_id  # distinguishable reason, points at original


async def test_concurrent_same_key_single_winner(db_path, repo_dir):
    """Threads racing on the same idempotency key produce exactly one record."""
    store = TaskStore(db_path)
    barrier = threading.Barrier(8)
    results: list[str] = []
    errors: list[Exception] = []

    def submit(i: int) -> None:
        rec = TaskRecord(task_id=f"t-{i}", idempotency_key="race-key",
                         request=ReviewRequest(repo_dir, ()))
        barrier.wait()
        try:
            results.append(store.insert_pending(rec).task_id)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=submit, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    assert len(set(results)) == 1, f"expected one winner, got {set(results)}"
    assert len(store.list_all()) == 1
    store.close()


async def test_duplicate_submission_does_not_reschedule(make_service, repo_dir):
    svc = make_service()
    await svc.start()
    first, _ = await svc.submit("key-1", repo_dir)
    await svc.submit("key-1", repo_dir)
    await svc.submit("key-1", repo_dir)
    final = await _wait_terminal(svc, first.task_id)
    assert final.state is TaskState.SUCCEEDED
    assert len(final.findings) == 1  # pipeline ran exactly once


async def _wait_terminal(svc, task_id: str):
    for _ in range(50):
        await asyncio.sleep(0.05)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not reach a terminal state")
