"""Pipeline failures map to distinguishable error categories, never silent."""
from __future__ import annotations

from review_runner.errors import ErrorCategory
from review_runner.models import ReviewRequest, TaskState
from review_runner.pipeline import ReviewPipeline
from tests.util import wait_for_state


def _submit(service, repo, rules=()):
    return service.submit_request(
        ReviewRequest(repo_path=str(repo), idempotency_key="k", rules=rules)
    )


def test_repo_not_found(make_service, tmp_path):
    service = make_service(repo_pipeline=ReviewPipeline())
    service.start()
    missing = tmp_path / "nope"
    task, _ = service.submit_request(
        ReviewRequest(repo_path=str(missing), idempotency_key="k")
    )
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.REPO_NOT_FOUND.value
    assert final.result is None


def test_unsupported_file_is_rejected_not_skipped(make_service, tmp_path):
    (tmp_path / "x.bin").write_bytes(b"\x00\x01\x02")
    service = make_service(repo_pipeline=ReviewPipeline())
    service.start()
    task, _ = _submit(service, tmp_path)
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.UNSUPPORTED_FILE.value
    assert "x.bin" in (final.error_message or "")


def test_malformed_utf8_is_malformed_input(make_service, tmp_path):
    (tmp_path / "bad.py").write_bytes(b"\xff\xfe not utf8")
    service = make_service(repo_pipeline=ReviewPipeline())
    service.start()
    task, _ = _submit(service, tmp_path)
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.MALFORMED_INPUT.value


def test_python_syntax_error_is_malformed_input(make_service, tmp_path):
    (tmp_path / "bad.py").write_text("def f(:\n", encoding="utf-8")
    service = make_service(repo_pipeline=ReviewPipeline())
    service.start()
    task, _ = _submit(service, tmp_path)
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.MALFORMED_INPUT.value
    assert "bad.py" in (final.error_message or "")


def test_unknown_rule_is_validation_error(make_service, tmp_repo):
    service = make_service(repo_pipeline=ReviewPipeline())
    service.start()
    task, _ = _submit(service, tmp_repo, rules=("no-such-rule",))
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.VALIDATION_ERROR.value


def test_rule_execution_error_category(make_service, tmp_repo):
    pipeline = ReviewPipeline(fail_rule="line-length")
    service = make_service(repo_pipeline=pipeline)
    service.start()
    task, _ = _submit(service, tmp_repo, rules=("line-length",))
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.RULE_EXECUTION_ERROR.value
    assert "line-length" in (final.error_message or "")


def test_index_error_category(make_service, tmp_repo):
    pipeline = ReviewPipeline(fail_stage=ErrorCategory.INDEX_ERROR)
    service = make_service(repo_pipeline=pipeline)
    service.start()
    task, _ = _submit(service, tmp_repo)
    final = wait_for_state(service, task.task_id, TaskState.FAILED)
    assert final.error_category == ErrorCategory.INDEX_ERROR.value


def test_success_records_findings(make_service, tmp_repo):
    service = make_service(repo_pipeline=ReviewPipeline())
    service.start()
    task, _ = _submit(service, tmp_repo, rules=("todo-comment", "print-call"))
    final = wait_for_state(service, task.task_id, TaskState.SUCCEEDED)
    assert final.result is not None
    rule_ids = {f.rule_id for f in final.result.findings}
    assert rule_ids == {"todo-comment", "print-call"}
    assert final.result.files_scanned == 2
