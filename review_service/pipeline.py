"""Review pipeline: scope -> read -> parse/index -> per-file rules.

The unit of failure is a single file: unreadable or unsupported files are
recorded with a distinguishable FileFailReason while the remaining files are
still reviewed; only repository-level problems fail the whole task.
"""

from __future__ import annotations

import ast
import logging
import os
from collections.abc import Callable
from dataclasses import dataclass, field

from review_service.models import (
    CapacityConfig,
    ErrorCategory,
    FileDisposition,
    FileFailReason,
    FileResult,
    ScopeConfig,
    ScopeSummary,
)
from review_service.scope import (
    ScopeCapacityError,
    apply_capacity,
    discover_scoped,
    resolve_scope,
)

logger = logging.getLogger(__name__)


class StageError(Exception):
    """A repository-level stage failed; carries a distinguishable category."""

    def __init__(self, category: ErrorCategory, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category.value}: {detail}")


@dataclass
class PipelineResult:
    """Everything a task produced: flat findings, per-file rows, and summary."""

    findings: list[dict] = field(default_factory=list)
    file_results: list[FileResult] = field(default_factory=list)
    summary: ScopeSummary = field(default_factory=ScopeSummary)


#: Suffixes the service knows how to read as text. Python suffixes are also
#: parsed/indexed; other text suffixes get text-only rules (e.g. no-todo).
PYTHON_SUFFIXES = frozenset({".py", ".pyi"})
TEXT_SUFFIXES = frozenset({
    ".py", ".pyi", ".txt", ".md", ".rst", ".cfg", ".ini", ".toml",
    ".yaml", ".yml", ".json", ".xml", ".html", ".css", ".js", ".ts",
    ".sh", ".bash", ".zsh", ".env", ".gitignore", ".dockerfile",
})


# -- per-file stages --------------------------------------------------------


def _read_text(path: str) -> str:
    """Read a file as strict UTF-8 text or raise a categorized FileError."""
    try:
        with open(path, "rb") as fh:
            data = fh.read()
    except OSError as exc:
        raise FileError(FileFailReason.READ_ERROR, f"cannot read {path}: {exc}") from exc
    if b"\x00" in data:
        raise FileError(FileFailReason.NOT_TEXT, f"binary content (NUL byte) in {path}")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise FileError(
            FileFailReason.NOT_TEXT,
            f"{path} is not valid UTF-8 text: {exc}",
        ) from exc


class FileError(Exception):
    """A per-file failure with a distinguishable reason."""

    def __init__(self, reason: FileFailReason, detail: str) -> None:
        self.reason = reason
        self.detail = detail
        super().__init__(f"{reason.value}: {detail}")


def _parse_python(path: str, text: str) -> ast.Module:
    try:
        return ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise FileError(
            FileFailReason.PARSE_ERROR,
            f"syntax error in {path}:{exc.lineno}: {exc.msg}",
        ) from exc


def _index_python(path: str, tree: ast.Module) -> list[dict]:
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
    except Exception as exc:  # defensive: indexing is categorized per file
        raise FileError(FileFailReason.INDEX_ERROR, f"indexing {path}: {exc}") from exc
    return symbols


# -- rules ------------------------------------------------------------------

MAX_FUNCTION_LINES = 50


def _rule_no_todo(abspath: str, lines: list[str], _symbols: list[dict]) -> list[dict]:
    return [
        {"rule": "no-todo", "path": abspath, "line": i, "message": "TODO comment found"}
        for i, line in enumerate(lines, start=1)
        if "TODO" in line
    ]


def _rule_long_function(abspath: str, _lines: list[str],
                        symbols: list[dict]) -> list[dict]:
    findings = []
    for sym in symbols:
        if sym["kind"] in ("FunctionDef", "AsyncFunctionDef"):
            length = sym["end_line"] - sym["line"] + 1
            if length > MAX_FUNCTION_LINES:
                findings.append(
                    {
                        "rule": "long-function",
                        "path": abspath,
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

#: Text-only rules run on any readable text file; AST-dependent rules are
#: skipped on non-Python files.
TEXT_ONLY_RULES = frozenset({"no-todo"})


def _run_rules(abspath: str, suffix: str, lines: list[str],
               symbols: list[dict], selected: tuple[str, ...]) -> list[dict]:
    findings: list[dict] = []
    for rule_name in selected:
        if suffix not in PYTHON_SUFFIXES and rule_name not in TEXT_ONLY_RULES:
            continue  # rule needs an AST this file type cannot provide
        rule = _RULES[rule_name]
        try:
            findings.extend(rule(abspath, lines, symbols))
        except FileError:
            raise
        except Exception as exc:
            raise FileError(
                FileFailReason.RULE_ERROR, f"rule {rule_name} on {abspath}: {exc}"
            ) from exc
    return findings


# -- orchestration ----------------------------------------------------------


def run_pipeline(
    repo_path: str,
    rules: tuple[str, ...],
    scope: ScopeConfig | None = None,
    capacity: CapacityConfig | None = None,
) -> PipelineResult:
    """Run the scoped review. Raises StageError only for repository-level
    failures (bad path, unknown rules, no in-scope files, repo too large)."""
    selected = rules or DEFAULT_RULES
    unknown = [r for r in selected if r not in _RULES]
    if unknown:
        raise StageError(
            ErrorCategory.UNSUPPORTED_INPUT, f"unknown rules: {', '.join(unknown)}"
        )

    if not os.path.exists(repo_path):
        raise StageError(ErrorCategory.INPUT_INVALID,
                         f"path does not exist: {repo_path}")
    if not os.path.isdir(repo_path):
        raise StageError(ErrorCategory.UNSUPPORTED_INPUT,
                         f"not a directory: {repo_path}")

    resolved_scope = resolve_scope(scope or ScopeConfig())
    plan = discover_scoped(repo_path, resolved_scope)
    if not plan.included and not plan.excluded:
        raise StageError(
            ErrorCategory.UNSUPPORTED_INPUT,
            f"no files match the configured scope under {repo_path}",
        )

    cap = capacity or CapacityConfig()
    try:
        decision = apply_capacity(plan, cap)
    except ScopeCapacityError as exc:
        logger.info(
            "repo_path=%s rejected_by_capacity limit=%s file_count=%d total_bytes=%d",
            repo_path, exc.limit, exc.file_count, exc.total_bytes,
        )
        raise StageError(
            ErrorCategory.REPO_TOO_LARGE,
            f"{exc.detail} (policy={cap.overflow_policy.value})",
        ) from exc

    # User-excluded files: transparent, with the responsible rule recorded.
    file_results: list[FileResult] = [
        FileResult(
            path=entry.relpath,
            disposition=FileDisposition.EXCLUDED,
            excluded_by=entry.excluded_by,
        )
        for entry in plan.excluded
    ]
    # Capacity-truncated files are already fully described FileResults.
    file_results.extend(decision.skipped)

    findings: list[dict] = []
    for entry in decision.selected:
        abspath = os.path.join(repo_path, entry.relpath)
        result = FileResult(
            path=entry.relpath,
            disposition=FileDisposition.REVIEWED,
            size_bytes=entry.size_bytes,
        )
        try:
            suffix = os.path.splitext(entry.relpath)[1].lower()
            if suffix not in TEXT_SUFFIXES:
                raise FileError(
                    FileFailReason.UNSUPPORTED_FILE_TYPE,
                    f"{entry.relpath}: unsupported source type (suffix {suffix or '<none>'})",
                )
            text = _read_text(abspath)
            lines = text.splitlines()
            symbols: list[dict] = []
            if suffix in PYTHON_SUFFIXES:
                tree = _parse_python(abspath, text)
                symbols = _index_python(abspath, tree)
            file_findings = _run_rules(abspath, suffix, lines, symbols, selected)
        except FileError as exc:
            result.disposition = FileDisposition.FAILED
            result.fail_reason = exc.reason
            result.fail_detail = exc.detail
            logger.info(
                "repo_path=%s file=%s disposition=FAILED reason=%s",
                repo_path, entry.relpath, exc.reason.value,
            )
        else:
            result.findings = file_findings
            findings.extend(file_findings)
            logger.info(
                "repo_path=%s file=%s disposition=REVIEWED findings=%d",
                repo_path, entry.relpath, len(file_findings),
            )
        file_results.append(result)

    # Stable, reproducible order: repo-relative path.
    file_results.sort(key=lambda r: r.path)
    findings.sort(key=lambda f: (f["path"], f["line"], f["rule"]))

    summary = ScopeSummary(
        total_in_scope=len(plan.included) + len(plan.excluded),
        reviewed=sum(r.disposition is FileDisposition.REVIEWED for r in file_results),
        excluded=sum(r.disposition is FileDisposition.EXCLUDED for r in file_results),
        failed=sum(r.disposition is FileDisposition.FAILED for r in file_results),
        skipped=sum(r.disposition is FileDisposition.SKIPPED for r in file_results),
        truncated=decision.truncated,
        rejected_by_limit=decision.rejected_by_limit,
    )
    return PipelineResult(findings=findings, file_results=file_results,
                          summary=summary)
