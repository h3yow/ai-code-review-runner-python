"""Configurable review scope: filtering, per-file outcomes, determinism."""

from __future__ import annotations

import asyncio
import os

import pytest

from review_service.models import ErrorCategory, ScopeConfig, TaskState
from review_service.store import IdempotencyConflictError

pytestmark = pytest.mark.asyncio


async def _run_and_wait(svc, key: str, repo_path: str, rules=(), scope=None):
    rec, _ = await svc.submit(key, repo_path, rules, scope)
    for _ in range(50):
        await asyncio.sleep(0.05)
        final = await svc.get(rec.task_id)
        if final.state in (TaskState.SUCCEEDED, TaskState.FAILED):
            return final
    raise AssertionError("task did not finish")


@pytest.fixture()
def mixed_repo(tmp_path):
    """A repo exercising every per-file outcome at once."""
    repo = tmp_path / "repo"
    (repo / "src" / "pkg").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "ok.py").write_text("def f():\n    return 1  # TODO fix\n")
    (repo / "src" / "broken.py").write_text("def broken(:\n")
    (repo / "src" / "blob.py").write_bytes(b"\xff\xfe\x00\x01 not text")
    (repo / "src" / "notes.txt").write_text("plain text, not source\n")
    (repo / "src" / "pkg" / "nested.py").write_text("X = 1  # TODO nested\n")
    (repo / "tests" / "test_a.py").write_text("def test_a():\n    pass  # TODO\n")
    (repo / "skipme.py").write_text("Y = 2\n")
    return str(repo)


async def test_include_dirs_limits_scan_to_subdir(make_service, mixed_repo):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-dirs", mixed_repo, scope=ScopeConfig(include_dirs=("tests",)))
    assert final.state is TaskState.SUCCEEDED
    assert [f["path"] for f in final.file_results] == ["tests/test_a.py"]
    assert final.summary["reviewed"] == 1


async def test_recursive_false_skips_subdirectories(make_service, mixed_repo):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-flat", mixed_repo, scope=ScopeConfig(recursive=False))
    assert final.state is TaskState.SUCCEEDED
    # only top-level *.py files: skipme.py (src/, tests/ not descended into)
    assert [f["path"] for f in final.file_results] == ["skipme.py"]


async def test_include_extensions_and_names_filters(make_service, mixed_repo):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-ext", mixed_repo,
        scope=ScopeConfig(include_names=("notes.txt",), include_extensions=()))
    assert final.state is TaskState.SUCCEEDED
    (entry,) = final.file_results
    assert entry["path"] == "src/notes.txt"
    # matched by scope but not a supported source type
    assert entry["status"] == "FAILED"
    assert entry["error_category"] == ErrorCategory.UNSUPPORTED_INPUT.value


async def test_exclude_rule_reports_matching_rule(make_service, mixed_repo):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-excl", mixed_repo,
        scope=ScopeConfig(exclude=("skipme.py", "tests/*")))
    assert final.state is TaskState.SUCCEEDED
    by_path = {f["path"]: f for f in final.file_results}
    assert by_path["skipme.py"]["status"] == "EXCLUDED"
    assert by_path["skipme.py"]["excluded_by"] == "skipme.py"
    assert by_path["tests/test_a.py"]["status"] == "EXCLUDED"
    assert by_path["tests/test_a.py"]["excluded_by"] == "tests/*"
    assert final.summary["excluded"] == 2
    assert final.summary["has_skips_or_failures"] is True


async def test_every_candidate_has_an_outcome(make_service, mixed_repo):
    """Reviewed / excluded / failed: no file in scope disappears silently."""
    svc = make_service()
    await svc.start()
    scope = ScopeConfig(include_dirs=("src",), include_extensions=(),
                        exclude=("src/pkg/*",))
    final = await _run_and_wait(svc, "k-all", mixed_repo, scope=scope)
    assert final.state is TaskState.SUCCEEDED
    by_path = {f["path"]: f for f in final.file_results}
    assert set(by_path) == {
        "src/ok.py", "src/broken.py", "src/blob.py",
        "src/notes.txt", "src/pkg/nested.py",
    }
    assert by_path["src/ok.py"]["status"] == "REVIEWED"
    assert by_path["src/ok.py"]["findings"], "reviewed file carries findings"
    assert by_path["src/broken.py"]["error_category"] == ErrorCategory.PARSE_ERROR.value
    assert by_path["src/blob.py"]["error_category"] == ErrorCategory.NOT_TEXT.value
    assert (by_path["src/notes.txt"]["error_category"]
            == ErrorCategory.UNSUPPORTED_INPUT.value)
    assert by_path["src/pkg/nested.py"]["status"] == "EXCLUDED"
    assert by_path["src/pkg/nested.py"]["excluded_by"] == "src/pkg/*"
    # one-glance summary
    assert final.summary == {
        "total_candidates": 5,
        "reviewed": 1,
        "excluded": 1,
        "failed": 3,
        "truncated": False,
        "omitted_files": 0,
        "omitted_paths": [],
        "has_skips_or_failures": True,
    }
    # remaining reviewable files still produce their conclusions
    assert any(f["rule"] == "no-todo" and f["path"] == "src/ok.py"
               for f in final.findings)


async def test_unreadable_file_is_input_invalid(make_service, mixed_repo):
    """A dangling symlink inside the scope cannot be read -> per-file failure."""
    os.symlink("/no/such/target", os.path.join(mixed_repo, "src", "ghost.py"))
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-ghost", mixed_repo, scope=ScopeConfig(include_dirs=("src",)))
    assert final.state is TaskState.SUCCEEDED
    by_path = {f["path"]: f for f in final.file_results}
    assert by_path["src/ghost.py"]["status"] == "FAILED"
    assert (by_path["src/ghost.py"]["error_category"]
            == ErrorCategory.INPUT_INVALID.value)


async def test_results_are_stable_and_reproducible(make_service, mixed_repo):
    """Two identical submissions yield identical per-file output ordering."""
    svc = make_service()
    await svc.start()
    scope = ScopeConfig(exclude=("tests/*",))
    a = await _run_and_wait(svc, "k-det-a", mixed_repo, scope=scope)
    b = await _run_and_wait(svc, "k-det-b", mixed_repo, scope=scope)
    assert a.file_results == b.file_results
    assert a.findings == b.findings
    assert a.summary == b.summary
    paths = [f["path"] for f in a.file_results]
    assert paths == sorted(paths)


async def test_scope_matching_nothing_is_unsupported(make_service, mixed_repo):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-none", mixed_repo,
        scope=ScopeConfig(include_extensions=(".rb",)))
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.UNSUPPORTED_INPUT


async def test_missing_include_dir_is_input_invalid(make_service, mixed_repo):
    svc = make_service()
    await svc.start()
    final = await _run_and_wait(
        svc, "k-nodir", mixed_repo,
        scope=ScopeConfig(include_dirs=("no-such-subdir",)))
    assert final.state is TaskState.FAILED
    assert final.error_category is ErrorCategory.INPUT_INVALID


async def test_scope_is_part_of_idempotency_fingerprint(make_service, mixed_repo):
    """Same key + same scope replays; same key + different scope conflicts."""
    svc = make_service()
    await svc.start()
    scope = ScopeConfig(include_dirs=("src",))
    rec, created = await svc.submit("k-idem", mixed_repo, scope=scope)
    assert created is True
    replay, created = await svc.submit("k-idem", mixed_repo, scope=scope)
    assert created is False
    assert replay.task_id == rec.task_id
    with pytest.raises(IdempotencyConflictError):
        await svc.submit("k-idem", mixed_repo,
                         scope=ScopeConfig(include_dirs=("tests",)))
