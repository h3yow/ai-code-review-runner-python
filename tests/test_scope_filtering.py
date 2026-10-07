"""Configurable review scope: includes, excludes, recursion, stable order."""

from __future__ import annotations

import os

from review_service.models import ScopeConfig
from review_service.scope import discover_scoped, resolve_scope


def _make_repo(base) -> str:
    repo = str(base)
    os.makedirs(f"{repo}/src/pkg")
    os.makedirs(f"{repo}/tests")
    os.makedirs(f"{repo}/.git")
    os.makedirs(f"{repo}/__pycache__")
    files = [
        "a.py", "b.pyi", "readme.md", "notes.txt",
        "src/s.py", "src/pkg/p.py",
        "tests/t.py", "tests/t.cfg",
        ".git/config.py", "__pycache__/x.cpython.pyc",
    ]
    for f in files:
        p = os.path.join(repo, f)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as fh:
            fh.write("x\n")
    return repo


def test_default_scope_is_python_sources_and_skips_vcs(tmp_path):
    repo = _make_repo(tmp_path)
    plan = discover_scoped(repo, resolve_scope(ScopeConfig()))
    assert [e.relpath for e in plan.included] == [
        "a.py", "b.pyi", "src/pkg/p.py", "src/s.py", "tests/t.py",
    ]
    assert plan.excluded == []


def test_non_recursive_only_lists_top_level(tmp_path):
    repo = _make_repo(tmp_path)
    plan = discover_scoped(repo, resolve_scope(ScopeConfig(recursive=False)))
    assert [e.relpath for e in plan.included] == ["a.py", "b.pyi"]


def test_include_dirs_limits_to_subtree(tmp_path):
    repo = _make_repo(tmp_path)
    plan = discover_scoped(repo, resolve_scope(ScopeConfig(include_dirs=("src",))))
    assert [e.relpath for e in plan.included] == ["src/pkg/p.py", "src/s.py"]


def test_explicit_suffixes_cover_non_python_types(tmp_path):
    repo = _make_repo(tmp_path)
    scope = resolve_scope(ScopeConfig(include_suffixes=(".md", ".cfg", "txt")))
    plan = discover_scoped(repo, scope)
    assert [e.relpath for e in plan.included] == [
        "notes.txt", "readme.md", "tests/t.cfg",
    ]


def test_include_names_glob(tmp_path):
    repo = _make_repo(tmp_path)
    scope = resolve_scope(ScopeConfig(include_names=("readme.*",)))
    plan = discover_scoped(repo, scope)
    assert [e.relpath for e in plan.included] == ["readme.md"]


def test_exclude_patterns_are_transparent_and_named(tmp_path):
    repo = _make_repo(tmp_path)
    scope = resolve_scope(ScopeConfig(exclude_patterns=("tests/*", "*.pyi")))
    plan = discover_scoped(repo, scope)
    included = [e.relpath for e in plan.included]
    assert "tests/t.py" not in included
    assert "b.pyi" not in included
    assert "a.py" in included
    excluded = {e.relpath: e.excluded_by for e in plan.excluded}
    assert excluded["tests/t.py"] == "tests/*"
    assert excluded["b.pyi"] == "*.pyi"


def test_first_matching_exclude_pattern_wins_in_user_order(tmp_path):
    repo = _make_repo(tmp_path)
    scope = resolve_scope(ScopeConfig(exclude_patterns=("src/", "src/pkg/p.py")))
    plan = discover_scoped(repo, scope)
    by = {e.relpath: e.excluded_by for e in plan.excluded}
    assert by["src/s.py"] == "src/"
    assert by["src/pkg/p.py"] == "src/"


def test_order_is_stable_across_walks(tmp_path):
    repo = _make_repo(tmp_path)
    scope = resolve_scope(ScopeConfig())
    first = [e.relpath for e in discover_scoped(repo, scope).included]
    second = [e.relpath for e in discover_scoped(repo, scope).included]
    assert first == second
