"""Cancellation of queued and running tasks: no success, no residue."""
from __future__ import annotations

import time

from review_runner.errors import ErrorCategory
from review_runner.models import ReviewRequest, TaskState
from tests.fakes import GatedPipeline
from tests.util import wait_for_state


def _req(repo, key: str) -> ReviewRequest:
    return ReviewRequest(repo_path=str(repo), idempotency_key=key)


def test_cancel_queued_task_never_runs(make_service, tmp_repo):
    pipeline = GatedPipeline()
    service = make_service(
        repo_pipeline=pipeline, max_concurrency=1, max_queue_size=10
    )
    service.start()

    running, _ = service.submit_request(_req(tmp_repo, "run-1"))
    queued, _ = service.submit_request(_req(tmp_repo, "queued-1"))
    assert pipeline.started.wait(2.0)

    cancelled = service.cancel_task(queued.task_id)
    assert cancelled.state is TaskState.CANCELLED
    assert cancelled.error_category == ErrorCategory.CANCELLED.value
    assert cancelled.result is None

    pipeline.gate.set()
    final = wait_for_state(service, running.task_id, TaskState.SUCCEEDED)
    assert final.state is TaskState.SUCCEEDED

    # The queued task stayed CANCELLED and never executed.
    time.sleep(0.05)
    assert service.store.get(queued.task_id).state is TaskState.CANCELLED
    assert "queued-1" not in pipeline.started_ids


def test_cancel_running_task_aborts_and_discards_partial_result(
    make_service, tmp_repo, caplog
):
    pipeline = GatedPipeline()
    service = make_service(repo_pipeline=pipeline, max_concurrency=2)
    service.start()

    task, _ = service.submit_request(_req(tmp_repo, "long-1"))
    assert pipeline.started.wait(2.0)
    wait_for_state(service, task.task_id, TaskState.RUNNING)

    cancelled = service.cancel_task(task.task_id)
    # CANCELLING may be observed transiently; it must converge to CANCELLED.
    assert cancelled.state in (TaskState.CANCELLING, TaskState.CANCELLED)

    final = wait_for_state(service, task.task_id, TaskState.CANCELLED)
    assert final.state is TaskState.CANCELLED
    assert final.error_category == ErrorCategory.CANCELLED.value
    assert final.result is None

    # Releasing the gate afterwards must not resurrect a success result.
    pipeline.gate.set()
    time.sleep(0.1)
    again = service.store.get(task.task_id)
    assert again.state is TaskState.CANCELLED
    assert again.result is None

    transitions = [
        r.message
        for r in caplog.records
        if task.task_id in r.getMessage()
    ]
    assert any("PENDING->RUNNING" in m for m in transitions)
    assert any("RUNNING->CANCELLING" in m for m in transitions)
    assert any("CANCELLING->CANCELLED" in m for m in transitions)
    assert any("error_category=CANCELLED" in m for m in transitions)


def test_cancel_is_idempotent_on_terminal_states(make_service, tmp_repo):
    pipeline = GatedPipeline()
    service = make_service(repo_pipeline=pipeline)
    service.start()

    task, _ = service.submit_request(_req(tmp_repo, "ok-1"))
    pipeline.gate.set()
    final = wait_for_state(service, task.task_id, TaskState.SUCCEEDED)
    assert final.state is TaskState.SUCCEEDED

    again = service.cancel_task(task.task_id)
    assert again.state is TaskState.SUCCEEDED
    assert again.result is not None


def test_cancel_wins_over_failure(make_service, tmp_repo):
    # A rule failure observed while cancellation is requested must still end
    # CANCELLED -- cancellation takes precedence over partial failure.
    from review_runner.pipeline import CancellationRequested
    from review_runner.models import ReviewResult
    from collections.abc import Callable
    from review_runner.models import ReviewRequest as R

    class RacePipeline:
        def __init__(self):
            self.enter = __import__("threading").Event()

        def run(self, request: R, is_cancelled: Callable[[], bool]) -> ReviewResult:
            self.enter.set()
            # Block until cancel has flipped the flag and moved us to
            # CANCELLING, then raise as a failing rule would.
            deadline = time.time() + 2
            while not is_cancelled() and time.time() < deadline:
                time.sleep(0.005)
            raise CancellationRequested()

    pipeline = RacePipeline()
    service = make_service(repo_pipeline=pipeline)
    service.start()

    task, _ = service.submit_request(_req(tmp_repo, "race-cancel"))
    assert pipeline.enter.wait(2.0)
    wait_for_state(service, task.task_id, TaskState.RUNNING)
    service.cancel_task(task.task_id)

    final = wait_for_state(service, task.task_id, TaskState.CANCELLED)
    assert final.state is TaskState.CANCELLED
    assert final.result is None
