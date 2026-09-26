"""Idempotent submission: one key -> one result; conflicts are rejected."""
from __future__ import annotations

import pytest

from review_runner.errors import ErrorCategory, ReviewError
from review_runner.models import ReviewRequest, TaskState


def _req(repo, key: str, rules=("line-length",)) -> ReviewRequest:
    return ReviewRequest(repo_path=str(repo), idempotency_key=key, rules=rules)


def test_duplicate_submission_returns_single_task(make_service, tmp_repo, caplog):
    service = make_service()
    service.start()

    task1, created1 = service.submit_request(_req(tmp_repo, "key-1"))
    task2, created2 = service.submit_request(_req(tmp_repo, "key-1"))

    assert created1 is True
    assert created2 is False
    assert task1.task_id == task2.task_id
    assert task1.request_fingerprint == task2.request_fingerprint

    from tests.util import wait_for_state
    final = wait_for_state(service, task1.task_id, TaskState.SUCCEEDED, TaskState.FAILED)
    assert final.state is TaskState.SUCCEEDED

    # Logs carry identity, key and transitions.
    lifecycle = [r.message for r in caplog.records if "TASK" in r.message]
    assert any(task1.task_id in m and "key-1" in m for m in lifecycle)
    assert any("PENDING->RUNNING" in m for m in lifecycle)
    assert any("RUNNING->SUCCEEDED" in m for m in lifecycle)


def test_same_key_different_content_is_conflict(make_service, tmp_repo, tmp_path):
    service = make_service()
    service.start()

    task, created = service.submit_request(_req(tmp_repo, "dup-key"))
    assert created is True

    other_repo = tmp_path / "other"
    other_repo.mkdir()
    (other_repo / "a.py").write_text("x = 1\n", encoding="utf-8")

    with pytest.raises(ReviewError) as exc:
        service.submit_request(_req(other_repo, "dup-key"))
    assert exc.value.category is ErrorCategory.IDEMPOTENCY_CONFLICT
    assert "dup-key" in str(exc.value)

    # The original task is untouched.
    again, created_again = service.submit_request(_req(tmp_repo, "dup-key"))
    assert created_again is False
    assert again.task_id == task.task_id


def test_same_key_different_rules_is_conflict(make_service, tmp_repo):
    service = make_service()
    service.start()

    service.submit_request(_req(tmp_repo, "rules-key", rules=("line-length",)))
    with pytest.raises(ReviewError) as exc:
        service.submit_request(_req(tmp_repo, "rules-key", rules=("print-call",)))
    assert exc.value.category is ErrorCategory.IDEMPOTENCY_CONFLICT


def test_rule_order_does_not_change_fingerprint(make_service, tmp_repo):
    service = make_service()
    service.start()

    task1, _ = service.submit_request(
        _req(tmp_repo, "order-key", rules=("line-length", "print-call"))
    )
    task2, created = service.submit_request(
        _req(tmp_repo, "order-key", rules=("print-call", "line-length"))
    )
    assert created is False
    assert task2.task_id == task1.task_id


def test_invalid_requests_are_rejected_with_category(make_service, tmp_repo):
    service = make_service()
    with pytest.raises(ReviewError) as exc:
        ReviewRequest(repo_path=str(tmp_repo), idempotency_key="  ")
    assert exc.value.category is ErrorCategory.VALIDATION_ERROR

    with pytest.raises(ReviewError) as exc:
        ReviewRequest(repo_path="", idempotency_key="k")
    assert exc.value.category is ErrorCategory.VALIDATION_ERROR

    with pytest.raises(ReviewError) as exc:
        ReviewRequest(repo_path=str(tmp_repo), idempotency_key="k", rules=("",))
    assert exc.value.category is ErrorCategory.VALIDATION_ERROR


def test_unknown_task_lookup_is_distinguishable(make_service):
    service = make_service()
    with pytest.raises(ReviewError) as exc:
        service.get_task("does-not-exist")
    assert exc.value.category is ErrorCategory.TASK_NOT_FOUND
