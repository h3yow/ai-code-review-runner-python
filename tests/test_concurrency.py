"""Concurrency caps, queueing, QUEUE_FULL rejection and concurrent submits."""
from __future__ import annotations

import threading
import time

import pytest

from review_runner.errors import ErrorCategory, ReviewError
from review_runner.models import ReviewRequest, TaskState
from tests.fakes import GatedPipeline
from tests.util import wait_for_state


def _req(repo, i: int) -> ReviewRequest:
    return ReviewRequest(repo_path=str(repo), idempotency_key=f"k-{i}")


def test_at_most_max_concurrency_tasks_run_and_rest_queue(
    make_service, tmp_repo
):
    pipeline = GatedPipeline()
    service = make_service(
        repo_pipeline=pipeline, max_concurrency=2, max_queue_size=10
    )
    service.start()

    t1, _ = service.submit_request(_req(tmp_repo, 1))
    t2, _ = service.submit_request(_req(tmp_repo, 2))
    t3, _ = service.submit_request(_req(tmp_repo, 3))

    # Two running, one queued.
    assert pipeline.started.wait(2.0)
    deadline = time.time() + 2
    while len(pipeline.started_ids) < 2 and time.time() < deadline:
        time.sleep(0.01)
    assert len(pipeline.started_ids) == 2
    assert service.store.get(t3.task_id).state is TaskState.PENDING

    pipeline.gate.set()
    for t in (t1, t2, t3):
        final = wait_for_state(service, t.task_id, TaskState.SUCCEEDED)
        assert final.state is TaskState.SUCCEEDED
        assert final.result is not None
        assert final.error_category is None


def test_queue_full_is_explicitly_rejected_and_persisted(make_service, tmp_repo):
    pipeline = GatedPipeline()
    service = make_service(
        repo_pipeline=pipeline, max_concurrency=1, max_queue_size=1
    )
    service.start()

    t1, _ = service.submit_request(_req(tmp_repo, 1))  # running
    t2, _ = service.submit_request(_req(tmp_repo, 2))  # queued (cap 1)
    assert pipeline.started.wait(2.0)

    with pytest.raises(ReviewError) as exc:
        service.submit_request(_req(tmp_repo, 3))  # overflow
    assert exc.value.category is ErrorCategory.QUEUE_FULL

    # The rejection is the deterministic persisted outcome for key k-3.
    t3 = service.get_by_idempotency_key("k-3")
    assert t3.state is TaskState.FAILED
    assert t3.error_category == ErrorCategory.QUEUE_FULL.value
    assert t3.result is None

    # A repeat submission for k-3 returns the same FAILED record, not a rerun.
    t3_again, created = service.submit_request(_req(tmp_repo, 3))
    assert created is False
    assert t3_again.task_id == t3.task_id

    pipeline.gate.set()
    wait_for_state(service, t1.task_id, TaskState.SUCCEEDED)
    wait_for_state(service, t2.task_id, TaskState.SUCCEEDED)


def test_concurrent_identical_submissions_produce_single_task(
    make_service, tmp_repo
):
    pipeline = GatedPipeline()
    service = make_service(repo_pipeline=pipeline, max_concurrency=4)
    service.start()

    req = ReviewRequest(repo_path=str(tmp_repo), idempotency_key="race-key")
    results: list[tuple] = []
    errors: list[ReviewError] = []

    def submit() -> None:
        try:
            results.append(service.submit_request(req))
        except ReviewError as exc:  # pragma: no cover - no conflict expected
            errors.append(exc)

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5.0)

    assert not errors
    task_ids = {t.task_id for t, _ in results}
    assert len(task_ids) == 1
    assert sum(1 for _, created in results if created) == 1

    only_id = next(iter(task_ids))
    pipeline.gate.set()
    final = wait_for_state(service, only_id, TaskState.SUCCEEDED)
    assert final.state is TaskState.SUCCEEDED
