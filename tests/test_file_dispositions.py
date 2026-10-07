"""Per-file transparency: reviewed / excluded / failed / skipped, nothing lost."""

from __future__ import annotations

import asyncio
import os

import pytest

from review_service.models import (
    FileDisposition,
    FileFailReason,
    ScopeConfig,
    TaskState,
)
from review_service.pipeline import run_pipeline



def _mixed_repo(base) -> str:
    repo = str(base)
    os.makedirs(f"{repo}/vendor")
    os.makedirs(f"{repo}/src")
    files = {
        "src/clean.py": "def f():\n    return 1\n",
        "src/todo.py": "# TODO clean this\nx = 1\n",
        "src/broken.py": "def broken(:\n",
        "src/binary.py": b"\x00\x01\x02\x03not text\xff",
        "vendor/v.py": "VENDOR = 1\n",
        "data.dat": b"hello\n",
        "notes.md": "# TODO header\n",
    }
    for name, content in files.items():
        path = os.path.join(repo, name)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        mode = "wb" if isinstance(content, bytes) else "w"
        with open(path, mode) as fh:
            fh.write(content)
    return repo


def _by_path(result):
    return {fr.path: fr for fr in result.file_results}


async def _wait_terminal(svc, task_id: str):
    for _ in range(100):
        await asyncio.sleep(0.03)
        rec = await svc.get(task_id)
        if rec.state in (TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED):
            return rec
    raise AssertionError(f"task {task_id} did not finish")


def test_every_in_scope_file_has_a_disposition(tmp_path):
    repo = _mixed_repo(tmp_path)
    scope = ScopeConfig(
        include_suffixes=(".py", ".md", ".dat"),
        exclude_patterns=("vendor/",),
    )
    result = run_pipeline(repo, (), scope)
    paths = {fr.path for fr in result.file_results}
    expected = {
        "src/clean.py", "src/todo.py", "src/broken.py", "src/binary.py",
        "vendor/v.py", "data.dat", "notes.md",
    }
    assert paths == expected  # nothing silently disappeared
    assert result.summary.total_in_scope == 7


def test_reviewed_files_carry_their_own_findings(tmp_path):
    repo = _mixed_repo(tmp_path)
    result = run_pipeline(
        repo, (), ScopeConfig(include_suffixes=(".py", ".md", ".dat"),
                              exclude_patterns=("vendor/",))
    )
    rows = _by_path(result)
    clean = rows["src/clean.py"]
    assert clean.disposition is FileDisposition.REVIEWED
    assert clean.findings == []
    todo = rows["src/todo.py"]
    assert todo.disposition is FileDisposition.REVIEWED
    assert len(todo.findings) == 1
    assert todo.findings[0]["rule"] == "no-todo"
    # findings path points at the file they came from
    assert all(f["path"].endswith("src/todo.py") for f in todo.findings)


def test_excluded_files_record_the_responsible_rule(tmp_path):
    repo = _mixed_repo(tmp_path)
    result = run_pipeline(
        repo, (), ScopeConfig(include_suffixes=(".py",),
                              exclude_patterns=("vendor/",))
    )
    vendored = _by_path(result)["vendor/v.py"]
    assert vendored.disposition is FileDisposition.EXCLUDED
    assert vendored.excluded_by == "vendor/"
    assert result.summary.excluded == 1


def test_binary_file_is_not_text_not_whole_task_failure(tmp_path):
    repo = _mixed_repo(tmp_path)
    result = run_pipeline(
        repo, (), ScopeConfig(include_suffixes=(".py",),
                              exclude_patterns=("vendor/",))
    )
    rows = _by_path(result)
    binary = rows["src/binary.py"]
    assert binary.disposition is FileDisposition.FAILED
    assert binary.fail_reason is FileFailReason.NOT_TEXT
    assert "not valid UTF-8" in (binary.fail_detail or "") or \
           "binary" in (binary.fail_detail or "")
    # sibling files still reviewed -> task-level result is still produced
    assert rows["src/clean.py"].disposition is FileDisposition.REVIEWED
    assert result.summary.failed == 2  # binary (NOT_TEXT) + broken (PARSE)


def test_unsupported_type_is_distinguishable_from_not_text(tmp_path):
    repo = _mixed_repo(tmp_path)
    # .dat is plain readable text? make it text to isolate the type reason
    with open(os.path.join(repo, "data.dat"), "w") as fh:
        fh.write("plain readable text\n")
    result = run_pipeline(
        repo, (), ScopeConfig(include_suffixes=(".py", ".md", ".dat"),
                              exclude_patterns=("vendor/",))
    )
    dat = _by_path(result)["data.dat"]
    assert dat.disposition is FileDisposition.FAILED
    assert dat.fail_reason is FileFailReason.UNSUPPORTED_FILE_TYPE
    assert dat.fail_reason is not FileFailReason.NOT_TEXT


def test_parse_failure_is_a_per_file_reason(tmp_path):
    repo = _mixed_repo(tmp_path)
    result = run_pipeline(
        repo, (), ScopeConfig(include_suffixes=(".py",),
                              exclude_patterns=("vendor/",))
    )
    broken = _by_path(result)["src/broken.py"]
    assert broken.disposition is FileDisposition.FAILED
    assert broken.fail_reason is FileFailReason.PARSE_ERROR


def test_summary_flags_failures_and_reviewed_findings_still_emitted(tmp_path):
    repo = _mixed_repo(tmp_path)
    result = run_pipeline(
        repo, (), ScopeConfig(include_suffixes=(".py", ".md", ".dat"),
                              exclude_patterns=("vendor/",))
    )
    s = result.summary
    assert s.reviewed == 3       # clean, todo, notes.md
    assert s.failed == 3         # binary.py NOT_TEXT, broken.py PARSE, data.dat
    assert s.excluded == 1       # vendor/v.py
    assert s.has_skips_or_failures
    # flat findings still produced for everything that was reviewable
    todo_findings = [f for f in result.findings if "todo.py" in f["path"]]
    notes_findings = [f for f in result.findings if f["path"].endswith("notes.md")]
    assert todo_findings and notes_findings


def test_clean_run_summary_has_no_failure_flag(tmp_path):
    repo = str(tmp_path)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("x = 1\n")
    result = run_pipeline(repo, (), ScopeConfig())
    assert result.summary.failed == 0
    assert result.summary.skipped == 0
    assert result.summary.truncated is False
    assert result.summary.has_skips_or_failures is False


@pytest.mark.asyncio
async def test_service_task_succeeds_even_when_some_files_fail(make_service, tmp_path):
    repo = _mixed_repo(tmp_path)
    svc = make_service()
    await svc.start()
    rec, _ = await svc.submit(
        "mixed-1", repo,
        scope=ScopeConfig(include_suffixes=(".py", ".md", ".dat"),
                          exclude_patterns=("vendor/",)),
    )
    final = await _wait_terminal(svc, rec.task_id)
    assert final.state is TaskState.SUCCEEDED  # whole order not yellow
    assert final.summary is not None
    assert final.summary.failed == 3
    assert final.summary.excluded == 1
    persisted = {fr.path: fr for fr in final.file_results}
    assert persisted["src/binary.py"].fail_reason is FileFailReason.NOT_TEXT
    assert persisted["vendor/v.py"].excluded_by == "vendor/"


def test_all_files_excluded_is_transparent_success(tmp_path):
    """Everything blocked by user rules: nothing fails, nothing disappears."""
    from review_service.pipeline import run_pipeline
    repo = str(tmp_path)
    with open(os.path.join(repo, "a.py"), "w") as fh:
        fh.write("x = 1\n")
    result = run_pipeline(repo, (), ScopeConfig(exclude_patterns=("*.py",)))
    assert result.summary.reviewed == 0
    assert result.summary.excluded == 1
    assert result.summary.failed == 0
    assert result.summary.total_in_scope == 1
    assert result.file_results[0].excluded_by == "*.py"
