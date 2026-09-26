"""Restart recovery: in-flight tasks converge; finished results are not recounted."""

from __future__ import annotations

import asyncio

import pytest

from review_service.models import (
    ErrorCategory,
    ReviewRequest,
    TaskRecord,
    TaskState,
)
from review_service.scheduler import Scheduler
from review_service.service import ReviewService
from review_service.store import TaskStore

pytestmark = pytest.mark.asyncio


def _seed_store(db_path: str, repo_dir: str) -> dict[str, str]:
    """Simulate a crashed run: one QUEUED, one RUNNING (with partial
    findings), one SUCCEEDED task persisted in the store."""
    store = TaskStore(db_path)
    ids = {}
    for key, target in (("k-queued", TaskState.QUEUED),
                        ("k-running", TaskState.RUNNING),
                        ("k-done", TaskState.SUCCEEDED)):
        rec = TaskRecord(task_id=f"task-{key}", idempotency_key=key,
                         request=ReviewRequest(repo_dir, ()))
        store.insert_pending(rec)
        store.transition(rec.task_id, TaskState.QUEUED)
        ids[key] = rec.task_id
    store.transition(ids["k-running"], TaskState.RUNNING)
    # partial findings left behind by the interrupted run
    store._conn.execute("UPDATE tasks SET findings='[{\"stale\": 1}]' WHERE task_id=?",
                        (ids["k-running"],))
    store._conn.commit()
    store.transition(ids["k-done"], TaskState.RUNNING)
    store.transition(ids["k-done"], TaskState.SUCCEEDED,
                     findings=[{"rule": "no-todo", "path": "p", "line": 1,
                                "message": "kept"}])
    store.close()
    return ids


async def test_restart_recovers_inflight_tasks(db_path, repo_dir):
    ids = _seed_store(db_path, repo_dir)

    svc = ReviewService(TaskStore(db_path), Scheduler(2, 8))
    await svc.start()  # recovery happens here

    for key in ("k-queued", "k-running"):
        final = await _wait_terminal(svc, ids[key])
        assert final.state is TaskState.SUCCEEDED, (key, final)
        # exactly one run's worth of findings: stale partial results cleared
        assert final.findings == [
            {"rule": "no-todo", "path": f"{repo_dir}/ok.py", "line": 2,
             "message": "TODO comment found"}
        ]

    done = await svc.get(ids["k-done"])
    assert done.state is TaskState.SUCCEEDED
    assert done.findings == [{"rule": "no-todo", "path": "p", "line": 1,
                              "message": "kept"}]  # not re-run, not recounted
    await svc.shutdown()


async def test_recovered_task_keeps_idempotency_key(db_path, repo_dir):
    ids = _seed_store(db_path, repo_dir)
    svc = ReviewService(TaskStore(db_path), Scheduler(2, 8))
    await svc.start()

    # replaying the same key after restart returns the recovered task
    replay, created = await svc.submit("k-queued", repo_dir)
    assert created is False
    assert replay.task_id == ids["k-queued"]
    await svc.shutdown()


async def _wait_terminal(svc, task_id: str):
    for _ in range(50):
        await asyncio.sleep(0.05)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not reach a terminal state")
