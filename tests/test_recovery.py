"""Recovery after a process restart: convergence without double counting."""
from __future__ import annotations

from collections.abc import Callable

from review_runner.errors import ErrorCategory
from review_runner.models import (
    ReviewRequest,
    ReviewResult,
    Task,
    TaskState,
)
from tests.util import wait_for_state


class CountingPipeline:
    """Records every actual pipeline execution per idempotency key."""

    def __init__(self) -> None:
        self.runs: list[str] = []

    def run(self, request: ReviewRequest, is_cancelled: Callable[[], bool]):
        self.runs.append(request.idempotency_key)
        return ReviewResult(findings=[], files_scanned=1)


def _req(repo, key: str = "k") -> ReviewRequest:
    return ReviewRequest(repo_path=str(repo), idempotency_key=key)


def test_pending_task_is_reenqueued_and_completes_after_restart(
    make_service, tmp_repo, tmp_path
):
    store_path = tmp_path / "tasks.json"
    service = make_service(name="tasks.json")  # auto_start=False
    task = Task.create(_req(tmp_repo, "pending-1"))
    service.store.insert_if_absent(task)
    service.shutdown()

    pipeline = CountingPipeline()
    restarted = make_service(repo_pipeline=pipeline, name="tasks.json")
    restarted.start()
    from tests.util import wait_for_state
    final = wait_for_state(restarted, task.task_id, TaskState.SUCCEEDED)
    assert final.state is TaskState.SUCCEEDED
    assert pipeline.runs.count("pending-1") == 1


def test_running_task_at_crash_is_interrupted_and_not_rerun(
    make_service, tmp_repo
):
    service = make_service(name="crash.json")  # auto_start=False
    task = Task.create(_req(tmp_repo, "running-1"))
    service.store.insert_if_absent(task)
    service.store.transition(task.task_id, task.version, TaskState.RUNNING)
    service.shutdown()

    pipeline = CountingPipeline()
    restarted = make_service(repo_pipeline=pipeline, name="crash.json")
    restarted.start()

    recovered = restarted.get_task(task.task_id)
    assert recovered.state is TaskState.FAILED
    assert recovered.error_category == ErrorCategory.INTERRUPTED.value
    assert recovered.result is None
    assert pipeline.runs == []  # interrupted work is never auto-resumed twice

    # A repeat submission for the same key returns the interrupted record
    # rather than producing a second result.
    again, created = restarted.submit_request(_req(tmp_repo, "running-1"))
    assert created is False
    assert again.task_id == task.task_id
    assert again.state is TaskState.FAILED
    assert pipeline.runs == []


def test_succeeded_task_stays_succeeded_and_is_not_reexecuted(
    make_service, tmp_repo
):
    pipeline1 = CountingPipeline()
    service = make_service(repo_pipeline=pipeline1, name="done.json")
    service.start()
    task, _ = service.submit_request(_req(tmp_repo, "done-1"))
    from tests.util import wait_for_state
    wait_for_state(service, task.task_id, TaskState.SUCCEEDED)
    assert pipeline1.runs == ["done-1"]
    service.shutdown()

    pipeline2 = CountingPipeline()
    restarted = make_service(repo_pipeline=pipeline2, name="done.json")
    restarted.start()
    import time
    time.sleep(0.05)
    final = restarted.get_task(task.task_id)
    assert final.state is TaskState.SUCCEEDED
    assert final.result is not None
    assert pipeline2.runs == []  # conclusions are never re-counted

    # Idempotent lookup still serves the single stored result.
    same, created = restarted.submit_request(_req(tmp_repo, "done-1"))
    assert created is False
    assert same.task_id == task.task_id


def test_cancelling_at_crash_is_interrupted(make_service, tmp_repo):
    service = make_service(name="cancelling.json")
    task = Task.create(_req(tmp_repo, "cancelling-1"))
    service.store.insert_if_absent(task)
    v = task.version
    service.store.transition(task.task_id, v, TaskState.RUNNING)
    running = service.store.get(task.task_id)
    service.store.transition(
        task.task_id, running.version, TaskState.CANCELLING
    )
    service.shutdown()

    pipeline = CountingPipeline()
    restarted = make_service(repo_pipeline=pipeline, name="cancelling.json")
    restarted.start()
    final = restarted.get_task(task.task_id)
    assert final.state is TaskState.FAILED
    assert final.error_category == ErrorCategory.INTERRUPTED.value
    assert pipeline.runs == []


def test_graceful_shutdown_keeps_queued_task_pending_then_completes(
    make_service, tmp_repo
):
    from tests.fakes import GatedPipeline
    import time

    pipeline = GatedPipeline()
    service = make_service(
        repo_pipeline=pipeline, max_concurrency=1, max_queue_size=5,
        name="shutdown.json",
    )
    service.start()
    running, _ = service.submit_request(_req(tmp_repo, "run-q"))
    queued, _ = service.submit_request(_req(tmp_repo, "wait-q"))
    assert pipeline.started.wait(2.0)
    pipeline.gate.set()  # let the RUNNING task finish so stop() is prompt

    service.shutdown()  # queued task must not be executed during drain
    assert service.store.get(queued.task_id).state is TaskState.PENDING
    assert "wait-q" not in pipeline.started_ids

    pipeline2 = CountingPipeline()
    restarted = make_service(
        repo_pipeline=pipeline2, max_concurrency=1, name="shutdown.json"
    )
    restarted.start()
    final = wait_for_state(restarted, queued.task_id, TaskState.SUCCEEDED)
    assert final.state is TaskState.SUCCEEDED
    assert pipeline2.runs == ["wait-q"]
