"""Scope/capacity participate in idempotency and survive persistence."""

from __future__ import annotations

import asyncio
import os

import pytest

from review_service.models import (
    CapacityConfig,
    FileDisposition,
    OverflowPolicy,
    ScopeConfig,
    TaskState,
)
from review_service.store import IdempotencyConflictError

pytestmark = pytest.mark.asyncio


async def _wait_terminal(svc, task_id: str):
    for _ in range(100):
        await asyncio.sleep(0.03)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not finish")


def _repo(base) -> str:
    repo = str(base / "repo")
    os.makedirs(f"{repo}/src")
    (base / "repo" / "a.py").write_text("# TODO\nx = 1\n")
    (base / "repo" / "src" / "b.py").write_text("y = 2\n")
    (base / "repo" / "note.md").write_text("# TODO md\n")
    return repo


async def test_same_scope_replay_runs_once(make_service, tmp_path):
    repo = _repo(tmp_path)
    svc = make_service()
    await svc.start()
    scope = ScopeConfig(include_suffixes=(".py", ".md"))
    first, c1 = await svc.submit("k-scope", repo, scope=scope)
    second, c2 = await svc.submit("k-scope", repo, scope=scope)
    assert c1 is True and c2 is False
    assert second.task_id == first.task_id
    final = await _wait_terminal(svc, first.task_id)
    assert final.state is TaskState.SUCCEEDED
    assert final.summary.reviewed == 3


async def test_different_scope_same_key_is_conflict(make_service, tmp_path):
    repo = _repo(tmp_path)
    svc = make_service()
    await svc.start()
    await svc.submit("k-s", repo, scope=ScopeConfig(include_suffixes=(".py",)))
    with pytest.raises(IdempotencyConflictError):
        await svc.submit("k-s", repo, scope=ScopeConfig(include_suffixes=(".md",)))


async def test_different_capacity_same_key_is_conflict(make_service, tmp_path):
    repo = _repo(tmp_path)
    svc = make_service()
    await svc.start()
    await svc.submit("k-c", repo, capacity=CapacityConfig(max_files=10))
    with pytest.raises(IdempotencyConflictError):
        await svc.submit("k-c", repo, capacity=CapacityConfig(max_files=5))


async def test_same_capacity_different_policy_is_conflict(make_service, tmp_path):
    repo = _repo(tmp_path)
    svc = make_service()
    await svc.start()
    await svc.submit(
        "k-p", repo,
        capacity=CapacityConfig(overflow_policy=OverflowPolicy.REJECT),
    )
    with pytest.raises(IdempotencyConflictError):
        await svc.submit(
            "k-p", repo,
            capacity=CapacityConfig(overflow_policy=OverflowPolicy.TRUNCATE),
        )


async def test_file_results_persist_across_store_reopen(
    make_service, db_path, tmp_path
):
    repo = _repo(tmp_path)
    svc = make_service(path=db_path)
    await svc.start()
    rec, _ = await svc.submit(
        "k-persist", repo,
        scope=ScopeConfig(include_suffixes=(".py", ".md"),
                          exclude_patterns=("src/",)),
    )
    final = await _wait_terminal(svc, rec.task_id)
    await svc.shutdown()

    # reopen a fresh store over the same SQLite file
    svc2 = make_service(path=db_path)  # same path, fresh connection
    reloaded = await svc2.get(final.task_id)
    assert reloaded is not None
    assert reloaded.summary.reviewed == 2  # a.py, note.md
    assert reloaded.summary.excluded == 1  # src/b.py
    rows = {fr.path: fr for fr in reloaded.file_results}
    assert rows["src/b.py"].disposition is FileDisposition.EXCLUDED
    assert rows["src/b.py"].excluded_by == "src/"
    assert reloaded.request.scope.exclude_patterns == ("src/",)
    assert rows["a.py"].findings[0]["rule"] == "no-todo"
