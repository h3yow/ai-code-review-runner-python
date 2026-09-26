"""Local review pipeline: repository parsing, indexing and rule execution.

Everything runs against the local filesystem: no network, credentials or
paid resources are involved. Each stage raises a ReviewError carrying a
distinct ErrorCategory; malformed/unsupported input is never ignored.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from review_runner.errors import ErrorCategory, ReviewError
from review_runner.models import ReviewFinding, ReviewRequest, ReviewResult

#: file suffixes we know how to parse; anything else is reported, not skipped.
SUPPORTED_SUFFIXES: frozenset[str] = frozenset({".py", ".txt", ".md", ".json"})

_LANGUAGE_BY_SUFFIX = {
    ".py": "python",
    ".txt": "text",
    ".md": "markdown",
    ".json": "json",
}

#: VCS / virtualenv / cache directories are not review targets.
_IGNORED_DIRS = frozenset(
    {".git", ".hg", ".svn", ".venv", "venv", "__pycache__",
     ".pytest_cache", ".ruff_cache", "node_modules"}
)

MAX_LINE_LENGTH = 120


class CancellationRequested(Exception):
    """Raised inside the pipeline when the task has been cancelled."""


@dataclass
class IndexedFile:
    rel_path: str
    language: str
    lines: list[str] = field(default_factory=list)
    size_bytes: int = 0


@dataclass
class RepositoryIndex:
    files: list[IndexedFile] = field(default_factory=list)


class Rule(Protocol):
    rule_id: str

    def evaluate(
        self, index: RepositoryIndex, is_cancelled: Callable[[], bool]
    ) -> list[ReviewFinding]: ...


def _check_cancelled(is_cancelled: Callable[[], bool]) -> None:
    if is_cancelled():
        raise CancellationRequested()


class LineLengthRule:
    rule_id = "line-length"

    def evaluate(self, index, is_cancelled):
        findings: list[ReviewFinding] = []
        for f in index.files:
            _check_cancelled(is_cancelled)
            for lineno, line in enumerate(f.lines, start=1):
                if len(line.rstrip("\n")) > MAX_LINE_LENGTH:
                    findings.append(
                        ReviewFinding(
                            rule_id=self.rule_id,
                            file_path=f.rel_path,
                            line=lineno,
                            message=f"line exceeds {MAX_LINE_LENGTH} characters",
                        )
                    )
        return findings


class TodoCommentRule:
    rule_id = "todo-comment"

    def evaluate(self, index, is_cancelled):
        findings: list[ReviewFinding] = []
        markers = ("TODO", "FIXME", "XXX")
        for f in index.files:
            _check_cancelled(is_cancelled)
            for lineno, line in enumerate(f.lines, start=1):
                stripped = line.strip()
                if any(m in stripped for m in markers):
                    findings.append(
                        ReviewFinding(
                            rule_id=self.rule_id,
                            file_path=f.rel_path,
                            line=lineno,
                            message="unresolved TODO/FIXME marker",
                        )
                    )
        return findings


class PrintCallRule:
    rule_id = "print-call"

    def evaluate(self, index, is_cancelled):
        findings: list[ReviewFinding] = []
        for f in index.files:
            if f.language != "python":
                continue
            _check_cancelled(is_cancelled)
            for lineno, line in enumerate(f.lines, start=1):
                stripped = line.strip()
                if stripped.startswith("print(") or " print(" in line:
                    findings.append(
                        ReviewFinding(
                            rule_id=self.rule_id,
                            file_path=f.rel_path,
                            line=lineno,
                            message="avoid print() in library code",
                        )
                    )
        return findings


RULE_REGISTRY: dict[str, Rule] = {
    r.rule_id: r
    for r in (LineLengthRule(), TodoCommentRule(), PrintCallRule())
}

DEFAULT_RULE_IDS: tuple[str, ...] = tuple(RULE_REGISTRY)


class ReviewPipeline:
    """Runs parse -> index -> rule steps without any external service."""

    def __init__(
        self,
        fail_rule: str | None = None,
        fail_stage: ErrorCategory | None = None,
    ) -> None:
        self.fail_rule = fail_rule
        self.fail_stage = fail_stage

    def run(
        self,
        request: ReviewRequest,
        is_cancelled: Callable[[], bool],
    ) -> ReviewResult:
        _check_cancelled(is_cancelled)
        files = self.parse_repository(request)
        _check_cancelled(is_cancelled)
        index = self.build_index(files, is_cancelled)
        findings = self.execute_rules(index, request.rules, is_cancelled)
        return ReviewResult(findings=findings, files_scanned=len(index.files))

    # ---- stages -------------------------------------------------------
    def parse_repository(self, request: ReviewRequest) -> list[IndexedFile]:
        root = Path(request.repo_path)
        if not root.exists() or not root.is_dir():
            raise ReviewError(
                ErrorCategory.REPO_NOT_FOUND,
                f"repository path does not exist or is not a directory: {root}",
            )

        indexed: list[IndexedFile] = []
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in _IGNORED_DIRS)
            for name in sorted(filenames):
                path = Path(dirpath) / name
                suffix = path.suffix.lower()
                if suffix not in SUPPORTED_SUFFIXES:
                    # Unsupported input must be surfaced, never ignored silently.
                    raise ReviewError(
                        ErrorCategory.UNSUPPORTED_FILE,
                        f"unsupported file type {suffix or '<none>'}: "
                        f"{path.relative_to(root)}",
                    )
                rel_path = str(path.relative_to(root))
                try:
                    raw = path.read_bytes()
                    text = raw.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ReviewError(
                        ErrorCategory.MALFORMED_INPUT,
                        f"file is not valid UTF-8: {rel_path}",
                    ) from exc
                except OSError as exc:
                    raise ReviewError(
                        ErrorCategory.MALFORMED_INPUT,
                        f"cannot read file {rel_path}: {exc}",
                    ) from exc

                if suffix == ".py":
                    try:
                        ast.parse(text, filename=rel_path)
                    except SyntaxError as exc:
                        raise ReviewError(
                            ErrorCategory.MALFORMED_INPUT,
                            f"python syntax error in {rel_path}:{exc.lineno}: "
                            f"{exc.msg}",
                        ) from exc

                indexed.append(
                    IndexedFile(
                        rel_path=rel_path,
                        language=_LANGUAGE_BY_SUFFIX[suffix],
                        lines=text.splitlines(),
                        size_bytes=len(raw),
                    )
                )
        return indexed

    def build_index(
        self, files: list[IndexedFile], is_cancelled: Callable[[], bool]
    ) -> RepositoryIndex:
        _check_cancelled(is_cancelled)
        if self.fail_stage is ErrorCategory.INDEX_ERROR:
            raise ReviewError(
                ErrorCategory.INDEX_ERROR, "injected index build failure"
            )
        index = RepositoryIndex(files=files)
        _check_cancelled(is_cancelled)
        return index

    def execute_rules(
        self,
        index: RepositoryIndex,
        rules: tuple[str, ...],
        is_cancelled: Callable[[], bool],
    ) -> list[ReviewFinding]:
        selected = rules or DEFAULT_RULE_IDS
        unknown = [r for r in selected if r not in RULE_REGISTRY]
        if unknown:
            raise ReviewError(
                ErrorCategory.VALIDATION_ERROR,
                f"unknown rule id(s): {', '.join(unknown)}",
            )

        findings: list[ReviewFinding] = []
        for rule_id in selected:
            _check_cancelled(is_cancelled)
            rule = RULE_REGISTRY[rule_id]
            if rule_id == self.fail_rule:
                raise ReviewError(
                    ErrorCategory.RULE_EXECUTION_ERROR,
                    f"injected failure while executing rule {rule_id}",
                )
            try:
                findings.extend(rule.evaluate(index, is_cancelled))
            except ReviewError:
                raise
            except CancellationRequested:
                raise
            except Exception as exc:  # defensive: categorize, never swallow
                raise ReviewError(
                    ErrorCategory.RULE_EXECUTION_ERROR,
                    f"rule {rule_id} failed: {exc}",
                ) from exc
        return findings
