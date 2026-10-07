"""Scope capacity limits: deterministic reject or marked truncation."""

from __future__ import annotations

import asyncio

import pytest

from review_service.models import (
    ErrorCategory,
    LimitPolicy,
    ReviewLimits,
    TaskState,
)

pytestmark = pytest.mark.asyncio


async def _run_and_wait(svc, key: str, repo_path: str):
    rec, _ = await svc.submit(key, repo_path)
    for _ in range(50):
        await asyncio.sleep(0.05)
        final = await svc.get(rec.task_id)
        if final.state in (TaskState.SUCCEEDED, TaskState.FAILED):
            return final
    raise AssertionError("task did not finish")


@pytest.fixture()
def wide_repo(tmp_path):
    """Five small python files, sorted order a..e."""
    repo = tmp_path / "repo"
    repo.mkdir()
    for name in "abcde":
        (repo / f"{name}.py").write_text(f"# {name}\nVALUE = 1  # TODO\n")
    return str(repo)


async def test_over_file_limit_rejects_with_distinguishable_category(
        make_service, wide_repo):
    svc = make_service(limits=ReviewLimits(max_files=2, policy=LimitPolicy.REJECT))
    await svc.start()
    final = await _run_and_wait(svc, "k-reject-files", wide_repo)
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.SCOPE_LIMIT_EXCEEDED
    assert "files=5/2" in (final.error_detail or "")
    # nothing half-done: no per-file results, no findings
    assert final.file_results == []
    assert final.findings == []


async def test_over_size_limit_rejects(make_service, wide_repo):
    svc = make_service(limits=ReviewLimits(max_total_bytes=10,
                                           policy=LimitPolicy.REJECT))
    await svc.start()
    final = await _run_and_wait(svc, "k-reject-bytes", wide_repo)
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.SCOPE_LIMIT_EXCEEDED
    assert "bytes=" in (final.error_detail or "")


async def test_truncate_policy_keeps_deterministic_prefix_and_marks_summary(
        make_service, wide_repo):
    limits = ReviewLimits(max_files=2, policy=LimitPolicy.TRUNCATE)
    svc = make_service(limits=limits)
    await svc.start()
    final = await _run_and_wait(svc, "k-trunc", wide_repo)
    assert final.state is TaskState.SUCCEEDED
    # deterministic: sorted-path prefix a.py, b.py survives
    assert [f["path"] for f in final.file_results] == ["a.py", "b.py"]
    assert final.summary["truncated"] is True
    assert final.summary["omitted_files"] == 3
    assert final.summary["omitted_paths"] == ["c.py", "d.py", "e.py"]
    assert final.summary["has_skips_or_failures"] is True
    # findings only from the kept prefix
    assert {f["path"] for f in final.findings} == {"a.py", "b.py"}


async def test_truncation_is_reproducible(make_service, wide_repo):
    limits = ReviewLimits(max_files=3, policy=LimitPolicy.TRUNCATE)
    svc = make_service(limits=limits)
    await svc.start()
    a = await _run_and_wait(svc, "k-tr-a", wide_repo)
    b = await _run_and_wait(svc, "k-tr-b", wide_repo)
    assert a.file_results == b.file_results
    assert a.summary == b.summary


async def test_within_limits_unaffected(make_service, wide_repo):
    svc = make_service(limits=ReviewLimits(max_files=10,
                                           policy=LimitPolicy.REJECT))
    await svc.start()
    final = await _run_and_wait(svc, "k-ok", wide_repo)
    assert final.state is TaskState.SUCCEEDED
    assert final.summary["reviewed"] == 5
    assert final.summary["truncated"] is False
    assert final.summary["has_skips_or_failures"] is False
