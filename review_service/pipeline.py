"""Review pipeline stages: parse -> index -> rules, over a local repository."""

from __future__ import annotations

import ast
import os
from collections.abc import Callable

from review_service.models import ErrorCategory


class StageError(Exception):
    """A pipeline stage failed; carries a distinguishable error category."""

    def __init__(self, category: ErrorCategory, detail: str) -> None:
        self.category = category
        self.detail = detail
        super().__init__(f"{category.value}: {detail}")


# -- stage 1: collect + parse ----------------------------------------------


def _collect_sources(repo_path: str) -> list[str]:
    if not os.path.exists(repo_path):
        raise StageError(ErrorCategory.INPUT_INVALID, f"path does not exist: {repo_path}")
    if not os.path.isdir(repo_path):
        raise StageError(ErrorCategory.UNSUPPORTED_INPUT, f"not a directory: {repo_path}")
    sources: list[str] = []
    for root, dirs, files in os.walk(repo_path):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
        for name in sorted(files):
            if name.endswith(".py"):
                sources.append(os.path.join(root, name))
    if not sources:
        raise StageError(
            ErrorCategory.UNSUPPORTED_INPUT,
            f"no supported source files (*.py) under {repo_path}",
        )
    return sources


def _parse(path: str) -> tuple[ast.Module, list[str]]:
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise StageError(ErrorCategory.INPUT_INVALID, f"cannot read {path}: {exc}") from exc
    try:
        tree = ast.parse(text, filename=path)
    except SyntaxError as exc:
        raise StageError(
            ErrorCategory.PARSE_ERROR, f"syntax error in {path}:{exc.lineno}: {exc.msg}"
        ) from exc
    return tree, text.splitlines()


# -- stage 2: index ---------------------------------------------------------


def _index(trees: dict[str, ast.Module]) -> dict[str, list[dict]]:
    index: dict[str, list[dict]] = {}
    for path, tree in trees.items():
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
        index[path] = symbols
    return index


# -- stage 3: rules ----------------------------------------------------------

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


def run_pipeline(repo_path: str, rules: tuple[str, ...]) -> list[dict]:
    """Execute all stages and return findings. Raises StageError on failure."""
    selected = rules or DEFAULT_RULES
    unknown = [r for r in selected if r not in _RULES]
    if unknown:
        raise StageError(
            ErrorCategory.UNSUPPORTED_INPUT, f"unknown rules: {', '.join(unknown)}"
        )

    sources = _collect_sources(repo_path)

    parsed: dict[str, tuple[ast.Module, list[str]]] = {}
    for path in sources:
        parsed[path] = _parse(path)
    trees = {p: t for p, (t, _lines) in parsed.items()}

    index = _index(trees)

    findings: list[dict] = []
    for rule_name in selected:
        rule = _RULES[rule_name]
        for path in sources:
            _tree, lines = parsed[path]
            try:
                findings.extend(rule(path, lines, index[path]))
            except StageError:
                raise
            except Exception as exc:
                raise StageError(
                    ErrorCategory.RULE_ERROR, f"rule {rule_name} on {path}: {exc}"
                ) from exc
    findings.sort(key=lambda f: (f["path"], f["line"], f["rule"]))
    return findings
