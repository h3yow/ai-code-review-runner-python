"""Review pipeline: scope enumeration -> per-file parse/index/rules.

Every file that enters the configured scope ends up in exactly one
per-file outcome (REVIEWED / EXCLUDED / FAILED); nothing is dropped
silently. Output ordering is deterministic (sorted by repo-relative path).
"""

from __future__ import annotations

import ast
import fnmatch
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from review_service.models import (
    ErrorCategory,
    FileStatus,
    LimitPolicy,
    ReviewLimits,
    ScopeConfig,
)

logger = logging.getLogger(__name__)

#: Extensions the review engine can actually analyse. Files matched by the
#: scope but outside this set get a per-file UNSUPPORTED_INPUT failure.
SUPPORTED_SOURCE_EXTENSIONS: tuple[str, ...] = (".py",)

#: Cap on how many omitted paths are echoed into the summary when truncating.
MAX_OMITTED_PATHS_LISTED = 100


class StageError(Exception):
    """A task-level pipeline failure; carries a distinguishable category."""

    def __init__(self, category: ErrorCategory, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category.value}: {detail}")


@dataclass
class PipelineResult:
    """Aggregate output of a pipeline run."""

    findings: list[dict] = field(default_factory=list)
    files: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)


# -- stage 0: scope enumeration ----------------------------------------------


def _iter_scope_files(repo_path: str, scope: ScopeConfig) -> list[str]:
    """Return repo-relative POSIX paths of files matching the include filters."""
    include_dirs = scope.include_dirs or ("",)
    matched: list[str] = []
    for inc in include_dirs:
        base = os.path.normpath(os.path.join(repo_path, inc))
        if not os.path.exists(base):
            raise StageError(
                ErrorCategory.INPUT_INVALID,
                f"include dir does not exist: {inc or '.'}",
            )
        if not os.path.isdir(base):
            raise StageError(
                ErrorCategory.INPUT_INVALID, f"include dir is not a directory: {inc}"
            )
        if scope.recursive:
            for root, dirs, files in os.walk(base):
                dirs[:] = sorted(
                    d for d in dirs if not d.startswith(".") and d != "__pycache__"
                )
                for name in sorted(files):
                    matched.append(os.path.join(root, name))
        else:
            for name in sorted(os.listdir(base)):
                full = os.path.join(base, name)
                if os.path.isfile(full):
                    matched.append(full)
    rel_paths = []
    for full in matched:
        name = os.path.basename(full)
        if scope.include_names and not any(
            fnmatch.fnmatch(name, pat) for pat in scope.include_names
        ):
            continue
        if scope.include_extensions and not name.endswith(scope.include_extensions):
            continue
        rel = os.path.relpath(full, repo_path).replace(os.sep, "/")
        rel_paths.append(rel)
    return sorted(set(rel_paths))


def _split_excluded(
    rel_paths: list[str], scope: ScopeConfig
) -> tuple[list[str], list[dict]]:
    """Split candidates into (kept, excluded-entries-with-matching-rule)."""
    kept: list[str] = []
    excluded: list[dict] = []
    for rel in rel_paths:
        rule = next((p for p in scope.exclude if fnmatch.fnmatch(rel, p)), None)
        if rule is None:
            kept.append(rel)
        else:
            excluded.append(
                {"path": rel, "status": FileStatus.EXCLUDED.value, "excluded_by": rule}
            )
    return kept, excluded


def _apply_limits(
    repo_path: str, kept: list[str], limits: ReviewLimits
) -> tuple[list[str], list[str]]:
    """Enforce file-count and total-bytes budgets.

    Returns (kept, omitted). Under REJECT, raising StageError instead.
    Truncation keeps the deterministic prefix in sorted-path order that fits
    both budgets.
    """
    sizes: dict[str, int] = {}
    total = 0
    for rel in kept:
        try:
            size = os.path.getsize(os.path.join(repo_path, rel))
        except OSError:
            size = 0  # unreadable files surface later as per-file failures
        sizes[rel] = size
        total += size
    over_files = len(kept) > limits.max_files
    over_bytes = total > limits.max_total_bytes
    if not over_files and not over_bytes:
        return kept, []
    detail = (
        f"scope exceeds limits: files={len(kept)}/{limits.max_files},"
        f" bytes={total}/{limits.max_total_bytes}"
    )
    if limits.policy is LimitPolicy.REJECT:
        raise StageError(ErrorCategory.SCOPE_LIMIT_EXCEEDED, detail)
    logger.warning("%s; truncating deterministically by sorted path", detail)
    kept_prefix: list[str] = []
    budget = 0
    for rel in kept:
        if len(kept_prefix) >= limits.max_files:
            break
        if budget + sizes[rel] > limits.max_total_bytes:
            continue  # skip oversized file, keep scanning smaller ones
        kept_prefix.append(rel)
        budget += sizes[rel]
    omitted = [rel for rel in kept if rel not in set(kept_prefix)]
    return kept_prefix, omitted


# -- per-file processing -------------------------------------------------------


def _file_failure(rel: str, category: ErrorCategory, detail: str) -> dict:
    logger.info("file=%s status=FAILED category=%s detail=%s", rel, category.value, detail)
    return {
        "path": rel,
        "status": FileStatus.FAILED.value,
        "error_category": category.value,
        "error_detail": detail,
    }


def _read_text(repo_path: str, rel: str) -> str:
    """Read a file as UTF-8 text, raising StageError with a per-file category."""
    full = os.path.join(repo_path, rel)
    try:
        with open(full, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise StageError(ErrorCategory.INPUT_INVALID, f"cannot read {rel}: {exc}") from exc
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StageError(
            ErrorCategory.NOT_TEXT, f"{rel} is not decodable as UTF-8 text: {exc}"
        ) from exc


def _parse(path: str, text: str) -> ast.Module:
    try:
        return ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise StageError(
            ErrorCategory.PARSE_ERROR, f"syntax error in {path}:{exc.lineno}: {exc.msg}"
        ) from exc


def _index(path: str, tree: ast.Module) -> list[dict]:
    symbols: list[dict] = []
    try:
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                symbols.append(
                    {
                        "name": node.name,
                        "kind": type(node).__name__,
                        "line": node.lineno,
                        "end_line": getattr(node, "end_lineno", node.lineno),
                    }
                )
    except Exception as exc:  # defensive: indexing must be categorized
        raise StageError(ErrorCategory.INDEX_ERROR, f"indexing {path}: {exc}") from exc
    return symbols


# -- rules ---------------------------------------------------------------------

MAX_FUNCTION_LINES = 50


def _rule_no_todo(path: str, lines: list[str], _symbols: list[dict]) -> list[dict]:
    return [
        {"rule": "no-todo", "path": path, "line": i, "message": "TODO comment found"}
        for i, line in enumerate(lines, start=1)
        if "TODO" in line
    ]


def _rule_long_function(path: str, _lines: list[str], symbols: list[dict]) -> list[dict]:
    findings = []
    for sym in symbols:
        if sym["kind"] in ("FunctionDef", "AsyncFunctionDef"):
            length = sym["end_line"] - sym["line"] + 1
            if length > MAX_FUNCTION_LINES:
                findings.append(
                    {
                        "rule": "long-function",
                        "path": path,
                        "line": sym["line"],
                        "message": f"function {sym['name']} spans {length} lines",
                    }
                )
    return findings


_RULES: dict[str, Callable[[str, list[str], list[dict]], list[dict]]] = {
    "no-todo": _rule_no_todo,
    "long-function": _rule_long_function,
}

DEFAULT_RULES: tuple[str, ...] = tuple(sorted(_RULES))


# -- entry point -----------------------------------------------------------------


def run_pipeline(
    repo_path: str,
    rules: tuple[str, ...],
    scope: ScopeConfig | None = None,
    limits: ReviewLimits | None = None,
) -> PipelineResult:
    """Execute the pipeline and return per-file outcomes plus aggregate findings.

    Task-level failures (bad repo path, unknown rules, empty scope, limit
    rejection) raise StageError. Per-file failures are captured in the
    per-file outcomes and never abort the run.
    """
    scope = scope or ScopeConfig()
    limits = limits or ReviewLimits()

    selected = rules or DEFAULT_RULES
    unknown = [r for r in selected if r not in _RULES]
    if unknown:
        raise StageError(
            ErrorCategory.UNSUPPORTED_INPUT, f"unknown rules: {', '.join(unknown)}"
        )

    if not os.path.exists(repo_path):
        raise StageError(ErrorCategory.INPUT_INVALID, f"path does not exist: {repo_path}")
    if not os.path.isdir(repo_path):
        raise StageError(ErrorCategory.UNSUPPORTED_INPUT, f"not a directory: {repo_path}")

    candidates = _iter_scope_files(repo_path, scope)
    kept, excluded_entries = _split_excluded(candidates, scope)
    if not candidates:
        raise StageError(
            ErrorCategory.UNSUPPORTED_INPUT,
            f"no files match the review scope under {repo_path}",
        )

    kept, omitted = _apply_limits(repo_path, kept, limits)

    file_results: list[dict] = list(excluded_entries)
    findings: list[dict] = []
    reviewed = 0
    failed = 0
    for rel in kept:
        if not rel.endswith(SUPPORTED_SOURCE_EXTENSIONS):
            failed += 1
            file_results.append(
                _file_failure(
                    rel, ErrorCategory.UNSUPPORTED_INPUT,
                    f"unsupported source type (engine supports"
                    f" {', '.join(SUPPORTED_SOURCE_EXTENSIONS)})",
                )
            )
            continue
        try:
            text = _read_text(repo_path, rel)
            tree = _parse(rel, text)
            symbols = _index(rel, tree)
        except StageError as exc:
            failed += 1
            file_results.append(_file_failure(rel, exc.category, exc.detail))
            continue
        lines = text.splitlines()
        file_findings: list[dict] = []
        rule_error: StageError | None = None
        for rule_name in selected:
            try:
                file_findings.extend(_RULES[rule_name](rel, lines, symbols))
            except Exception as exc:  # noqa: BLE001 - categorize per file
                rule_error = StageError(
                    ErrorCategory.RULE_ERROR, f"rule {rule_name} on {rel}: {exc}"
                )
                break
        if rule_error is not None:
            failed += 1
            file_results.append(
                _file_failure(rel, rule_error.category, rule_error.detail)
            )
            continue
        reviewed += 1
        findings.extend(file_findings)
        logger.info(
            "file=%s status=REVIEWED findings=%d", rel, len(file_findings)
        )
        file_results.append(
            {
                "path": rel,
                "status": FileStatus.REVIEWED.value,
                "findings": sorted(
                    file_findings, key=lambda f: (f["line"], f["rule"])
                ),
            }
        )

    for entry in excluded_entries:
        logger.info(
            "file=%s status=EXCLUDED excluded_by=%s", entry["path"], entry["excluded_by"]
        )

    file_results.sort(key=lambda e: e["path"])
    findings.sort(key=lambda f: (f["path"], f["line"], f["rule"]))
    summary = {
        "total_candidates": len(candidates),
        "reviewed": reviewed,
        "excluded": len(excluded_entries),
        "failed": failed,
        "truncated": bool(omitted),
        "omitted_files": len(omitted),
        "omitted_paths": sorted(omitted)[:MAX_OMITTED_PATHS_LISTED],
        "has_skips_or_failures": bool(excluded_entries or failed or omitted),
    }
    logger.info("pipeline summary: %s", summary)
    return PipelineResult(findings=findings, files=file_results, summary=summary)
