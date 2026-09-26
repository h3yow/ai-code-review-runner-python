"""File-backed store: persistence, CAS versions and recovery convergence."""
from __future__ import annotations

import pytest

from review_runner.errors import ErrorCategory
from review_runner.models import ReviewRequest, ReviewResult, Task, TaskState
from review_runner.storage import TaskStore


def _task(repo, key: str = "k") -> Task:
    return Task.create(ReviewRequest(repo_path=str(repo), idempotency_key=key))


def test_records_survive_reload(tmp_path, tmp_repo):
    path = tmp_path / "tasks.json"
    store = TaskStore(path)
    store.load()
    task = _task(tmp_repo)
    store.insert_if_absent(task)
    stored = store.get(task.task_id)
    store.transition(stored.task_id, stored.version, TaskState.RUNNING)
    running = store.get(task.task_id)
    store.transition(
        running.task_id,
        running.version,
        TaskState.SUCCEEDED,
        result=ReviewResult(files_scanned=3),
    )

    reloaded = TaskStore(path)
    reloaded.load()
    again = reloaded.get(task.task_id)
    assert again.state is TaskState.SUCCEEDED
    assert again.version == 2
    assert again.result is not None
    assert again.result.files_scanned == 3
    assert reloaded.get_by_idempotency_key("k").task_id == task.task_id


def test_stale_version_transition_is_rejected(tmp_path, tmp_repo):
    store = TaskStore(tmp_path / "t.json")
    task = _task(tmp_repo)
    store.insert_if_absent(task)
    store.transition(task.task_id, 0, TaskState.RUNNING)
    with pytest.raises(RuntimeError, match="stale version"):
        store.transition(task.task_id, 0, TaskState.SUCCEEDED)


def test_illegal_transition_is_rejected_even_with_fresh_version(
    tmp_path, tmp_repo
):
    store = TaskStore(tmp_path / "t.json")
    task = _task(tmp_repo)
    store.insert_if_absent(task)
    with pytest.raises(RuntimeError, match="illegal state transition"):
        store.transition(task.task_id, 0, TaskState.SUCCEEDED)


def test_recovery_marks_running_interrupted_and_keeps_pending(
    tmp_path, tmp_repo
):
    store = TaskStore(tmp_path / "t.json")
    t_pending = _task(tmp_repo, "p")
    t_running = _task(tmp_repo, "r")
    store.insert_if_absent(t_pending)
    store.insert_if_absent(t_running)
    r = store.get(t_running.task_id)
    store.transition(r.task_id, r.version, TaskState.RUNNING)

    requeue = store.recover_on_startup()
    assert [t.task_id for t in requeue] == [t_pending.task_id]

    interrupted = store.get(t_running.task_id)
    assert interrupted.state is TaskState.FAILED
    assert interrupted.error_category == ErrorCategory.INTERRUPTED.value

    # Recovery is idempotent on a second pass.
    assert store.recover_on_startup() == [t_pending]
    assert store.get(t_running.task_id).version == interrupted.version
