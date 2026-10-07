"""Capacity limits: deterministic reject or documented, labelled truncation."""

from __future__ import annotations

import asyncio
import os

import pytest

from review_service.models import (
    CapacityConfig,
    ErrorCategory,
    FileDisposition,
    FileFailReason,
    OverflowPolicy,
    ScopeConfig,
    TaskState,
)
from review_service.pipeline import StageError, run_pipeline


def _make_repo(base, n: int = 6, size: int = 10) -> str:
    repo = str(base)
    for i in range(n):
        with open(os.path.join(repo, f"f{i:02d}.py"), "wb") as fh:
            fh.write(b"# TODO\n" + b"x" * size)
    return repo


async def _wait_terminal(svc, task_id: str):
    for _ in range(100):
        await asyncio.sleep(0.03)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not finish")


def test_reject_when_file_count_exceeds_limit(tmp_path):
    repo = _make_repo(tmp_path, n=6)
    with pytest.raises(StageError) as exc:
        run_pipeline(repo, (), ScopeConfig(), CapacityConfig(max_files=5))
    assert exc.value.category is ErrorCategory.REPO_TOO_LARGE
    assert "max_files" in exc.value.detail


def test_reject_when_total_bytes_exceeds_limit(tmp_path):
    repo = _make_repo(tmp_path, n=3, size=100)
    with pytest.raises(StageError) as exc:
        run_pipeline(
            repo, (), ScopeConfig(), CapacityConfig(max_total_bytes=200)
        )
    assert exc.value.category is ErrorCategory.REPO_TOO_LARGE
    assert "max_total_bytes" in exc.value.detail


def test_truncate_keeps_stable_prefix_and_labels_the_rest(tmp_path):
    repo = _make_repo(tmp_path, n=6)
    result = run_pipeline(
        repo, (), ScopeConfig(),
        CapacityConfig(max_files=3, overflow_policy=OverflowPolicy.TRUNCATE),
    )
    by_disp = {fr.path: fr for fr in result.file_results}
    assert sorted(p for p, fr in by_disp.items()
                  if fr.disposition is FileDisposition.REVIEWED) == [
        "f00.py", "f01.py", "f02.py",
    ]
    skipped = [fr for fr in result.file_results
               if fr.disposition is FileDisposition.SKIPPED]
    assert [fr.path for fr in skipped] == ["f03.py", "f04.py", "f05.py"]
    assert all("max_files=3" in (fr.fail_detail or "") for fr in skipped)
    assert result.summary.truncated is True
    assert result.summary.rejected_by_limit == "max_files"
    assert result.summary.skipped == 3
    assert result.summary.reviewed == 3
    assert result.summary.total_in_scope == 6
    assert result.findings  # the prefix was still reviewed


def test_truncation_is_reproducible(tmp_path):
    repo = _make_repo(tmp_path, n=6)
    cap = CapacityConfig(max_files=3, overflow_policy=OverflowPolicy.TRUNCATE)
    r1 = run_pipeline(repo, (), ScopeConfig(), cap)
    r2 = run_pipeline(repo, (), ScopeConfig(), cap)
    paths1 = [(fr.path, fr.disposition.value) for fr in r1.file_results]
    paths2 = [(fr.path, fr.disposition.value) for fr in r2.file_results]
    assert paths1 == paths2  # same labelled prefix every time


def test_single_oversized_file_is_labelled_not_crashing(tmp_path):
    """Once the byte limit trips, later files are labelled SKIPPED too;
    the offending file carries the specific FILE_TOO_LARGE reason."""
    repo = str(tmp_path)
    with open(os.path.join(repo, "small.py"), "w") as fh:
        fh.write("x = 1\n")
    with open(os.path.join(repo, "huge.py"), "wb") as fh:
        fh.write(b"x" * 500)
    result = run_pipeline(
        repo, (), ScopeConfig(),
        CapacityConfig(max_total_bytes=100,
                       overflow_policy=OverflowPolicy.TRUNCATE),
    )
    by_path = {fr.path: fr for fr in result.file_results}
    assert by_path["huge.py"].disposition is FileDisposition.SKIPPED
    assert by_path["huge.py"].fail_reason is FileFailReason.FILE_TOO_LARGE
    assert by_path["small.py"].disposition is FileDisposition.SKIPPED
    assert by_path["small.py"].fail_reason is None
    assert result.summary.rejected_by_limit == "max_total_bytes"
    assert result.summary.reviewed == 0


@pytest.mark.asyncio
async def test_service_marks_reject_task_failed_with_category(make_service, tmp_path):
    repo = _make_repo(tmp_path, n=4)
    svc = make_service()
    await svc.start()
    rec, _ = await svc.submit(
        "cap-reject", repo,
        capacity=CapacityConfig(max_files=2),  # default policy: reject
    )
    final = await _wait_terminal(svc, rec.task_id)
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.REPO_TOO_LARGE
    assert final.findings == []  # rejected outright: nothing half-done


@pytest.mark.asyncio
async def test_large_batch_still_runs_under_concurrency_limit(
    make_service, tmp_path, monkeypatch
):
    """A bigger batch does not break the scheduler: it drains with max=1."""
    from tests.conftest import BlockingPipeline

    repo = _make_repo(tmp_path, n=8)
    blocker = BlockingPipeline()
    monkeypatch.setattr("review_service.service.run_pipeline", blocker)
    svc = make_service(max_concurrency=1, max_queue=8)
    await svc.start()

    records = [await svc.submit(f"batch-{i}", repo) for i in range(8)]
    await asyncio.to_thread(blocker.entered.wait, 10)
    assert svc._scheduler.running_count == 1
    assert svc._scheduler.queued_count == 7
    blocker.release.set()
    for rec, _ in records:
        final = await _wait_terminal(svc, rec.task_id)
        assert final.state is TaskState.SUCCEEDED
    assert len(blocker.calls) == 8  # every task executed exactly once
