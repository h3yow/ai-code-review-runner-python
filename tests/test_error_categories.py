"""Failure classification: each stage failure maps to a distinguishable category."""

from __future__ import annotations

import asyncio

import pytest

from review_service.models import ErrorCategory, TaskState

pytestmark = pytest.mark.asyncio


async def _run_and_wait(svc, key: str, repo_path: str, rules=()):
    rec, _ = await svc.submit(key, repo_path, rules)
    for _ in range(50):
        await asyncio.sleep(0.05)
        final = await svc.get(rec.task_id)
        if final.state in (TaskState.SUCCEEDED, TaskState.FAILED):
            return final
    raise AssertionError("task did not finish")


async def test_missing_path_is_input_invalid(make_service):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(svc, "k-missing", "/no/such/dir")
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.INPUT_INVALID
    assert "does not exist" in (final.error_detail or "")


async def test_empty_repo_is_unsupported(make_service, tmp_path):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(svc, "k-empty", str(tmp_path))
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.UNSUPPORTED_INPUT


async def test_unknown_rule_is_unsupported(make_service, repo_dir):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(svc, "k-rule", repo_dir, ("no-such-rule",))
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.UNSUPPORTED_INPUT


async def test_syntax_error_is_parse_error(make_service, tmp_path):
    """A single unparsable file no longer fails the whole task: it is recorded
    as a per-file PARSE_ERROR while sibling files are still reviewed."""
    from review_service.models import FileDisposition

    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "broken.py").write_text("def broken(:\n")
    (repo / "ok.py").write_text("x = 1  # TODO\n")
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(svc, "k-parse", str(repo))
    assert final.state is TaskState.SUCCEEDED  # task itself is not failed
    by_path = {fr.path: fr for fr in final.file_results}
    broken = by_path["broken.py"]
    assert broken.disposition is FileDisposition.FAILED
    assert broken.fail_reason.value == "PARSE_ERROR"
    assert "syntax error" in (broken.fail_detail or "")
    assert by_path["ok.py"].disposition is FileDisposition.REVIEWED
    assert final.summary.failed == 1
    assert final.summary.reviewed == 1
    assert final.summary.has_skips_or_failures


async def test_categories_are_distinguishable(make_service, tmp_path):
    """Different failures yield different, machine-readable categories."""
    svc = make_service()
    await svc.start()
    a = await _run_and_wait(svc, "k-a", "/no/such/dir")
    b = await _run_and_wait(svc, "k-b", str(tmp_path))
    assert a.error_category is not b.error_category
    assert a.error_category.value != b.error_category.value
