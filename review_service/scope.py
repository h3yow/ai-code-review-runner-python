"""Configurable review scope: deterministic file discovery and capacity limits."""

from __future__ import annotations

import fnmatch
import os
from dataclasses import dataclass

from review_service.models import (
    CapacityConfig,
    FileDisposition,
    FileFailReason,
    FileResult,
    OverflowPolicy,
    ScopeConfig,
)

#: Directory names always pruned before scope rules run; they are never
#: enumerated as in-scope (version control metadata, interpreter caches).
BUILTIN_PRUNE_DIRS = frozenset({".git", ".hg", ".svn", "__pycache__"})


@dataclass(frozen=True)
class ScopedEntry:
    """A file that landed inside the configured scope."""

    relpath: str
    size_bytes: int


@dataclass(frozen=True)
class ExcludedEntry:
    """A file blocked by a user-supplied exclude pattern."""

    relpath: str
    excluded_by: str


@dataclass
class ScopedPlan:
    """Every file encountered, split into included and user-excluded."""

    included: list[ScopedEntry]
    excluded: list[ExcludedEntry]


class ScopeCapacityError(Exception):
    """The in-scope plan deterministically exceeds the configured limits."""

    def __init__(self, detail: str, limit: str, file_count: int,
                 total_bytes: int) -> None:
        self.detail = detail
        self.limit = limit
        self.file_count = file_count
        self.total_bytes = total_bytes
        super().__init__(detail)


@dataclass
class CapacityDecision:
    """Result of applying capacity limits to a scoped plan."""

    selected: list[ScopedEntry]
    skipped: list[FileResult]
    truncated: bool
    rejected_by_limit: str | None


def _norm(path_parts: tuple[str, ...]) -> str:
    return "/".join(path_parts)


def _is_excluded(rel_parts: tuple[str, ...], patterns: tuple[str, ...]) -> str | None:
    """Return the first user pattern (in user order) that matches the path.

    Semantics:
    - a pattern ending in ``/`` is a directory subtree prefix;
    - a pattern containing ``/`` is a glob over the repo-relative POSIX path;
    - any other pattern is a glob over the file name only.
    """
    rel = _norm(rel_parts)
    name = rel_parts[-1]
    for pat in patterns:
        if pat.endswith("/"):
            anchored = pat.rstrip("/")
            if rel == anchored or rel.startswith(anchored + "/"):
                return pat
        elif "/" in pat:
            if fnmatch.fnmatch(rel, pat):
                return pat
        elif fnmatch.fnmatch(name, pat):
            return pat
    return None


def _is_included(
    rel_parts: tuple[str, ...],
    scope: ScopeConfig,
) -> bool:
    """Include rules are intersected: an active include list must match."""
    rel = _norm(rel_parts)
    name = rel_parts[-1]
    if scope.include_dirs:
        if not any(rel == d or rel.startswith(d.rstrip("/") + "/")
                   for d in scope.include_dirs):
            return False
    if scope.include_names and not any(
        fnmatch.fnmatch(name, n) for n in scope.include_names
    ):
        return False
    if scope.include_suffixes and not any(
        name.endswith(suf if suf.startswith(".") else "." + suf)
        for suf in scope.include_suffixes
    ):
        return False
    return True


def _norm_suffix(suf: str) -> str:
    suf = suf.strip()
    return suf if suf.startswith(".") else "." + suf


#: Default include set when the user specifies no include rule at all.
DEFAULT_SUFFIXES: tuple[str, ...] = (".py", ".pyi")


def resolve_scope(scope: ScopeConfig) -> ScopeConfig:
    """Normalize user input and apply the compatibility default.

    With no include rule supplied the default scope is Python sources
    (``.py``/``.pyi``), preserving the pre-scope-config behaviour. Any explicit
    include rule makes the user's rules authoritative.
    """
    suffixes = tuple(_norm_suffix(s) for s in scope.include_suffixes if s.strip())
    dirs = tuple(d.strip().strip("/") for d in scope.include_dirs if d.strip())
    names = tuple(n.strip() for n in scope.include_names if n.strip())
    if not dirs and not names and not suffixes:
        suffixes = DEFAULT_SUFFIXES
    return ScopeConfig(
        recursive=scope.recursive,
        include_dirs=dirs,
        include_names=names,
        include_suffixes=suffixes,
        exclude_patterns=tuple(p for p in scope.exclude_patterns if p.strip()),
    )


def discover_scoped(repo_path: str, scope: ScopeConfig) -> ScopedPlan:
    """Walk repo_path and partition files into included / user-excluded.

    A file is enumerated only if the include filters accept it. Accepted files
    are then checked against user exclude patterns: matches land in
    ``excluded`` (transparent), non-matches in ``included``. Order is
    repo-relative path order (stable and reproducible).
    """
    included: list[ScopedEntry] = []
    excluded: list[ExcludedEntry] = []

    def visit(dir_rel: tuple[str, ...], dir_abs: str) -> None:
        with os.scandir(dir_abs) as it:
            entries = list(it)
        for entry in sorted(entries, key=lambda e: e.name):
            rel_parts = dir_rel + (entry.name,)
            if entry.is_dir(follow_symlinks=False):
                if entry.name in BUILTIN_PRUNE_DIRS:
                    continue
                if scope.recursive:
                    visit(rel_parts, entry.path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue  # symlinks/sockets/... are not reviewable files
            try:
                size = entry.stat(follow_symlinks=False).st_size
            except OSError:
                size = 0  # read failure surfaces later as a per-file READ_ERROR
            if not _is_included(rel_parts, scope):
                continue
            blocked_by = _is_excluded(rel_parts, scope.exclude_patterns)
            if blocked_by is not None:
                excluded.append(ExcludedEntry(_norm(rel_parts), blocked_by))
            else:
                included.append(ScopedEntry(_norm(rel_parts), size))

    visit((), repo_path)
    included.sort(key=lambda e: e.relpath)
    excluded.sort(key=lambda e: e.relpath)
    return ScopedPlan(included=included, excluded=excluded)


def apply_capacity(plan: ScopedPlan, capacity: CapacityConfig) -> CapacityDecision:
    """Enforce max_files / max_total_bytes per the configured overflow policy.

    Limits are applied in a fixed order -- file count first, then cumulative
    bytes in repo-relative path order -- so the outcome is deterministic and
    reproducible. REJECT raises before any file is processed; TRUNCATE keeps
    the stable prefix and marks every leftover file as SKIPPED.
    """
    included = plan.included
    total_bytes = sum(e.size_bytes for e in included)

    if capacity.overflow_policy.value == OverflowPolicy.REJECT.value:
        if len(included) > capacity.max_files:
            raise ScopeCapacityError(
                f"in-scope file count {len(included)} exceeds max_files="
                f"{capacity.max_files}",
                limit="max_files", file_count=len(included),
                total_bytes=total_bytes,
            )
        if total_bytes > capacity.max_total_bytes:
            raise ScopeCapacityError(
                f"in-scope total size {total_bytes} bytes exceeds "
                f"max_total_bytes={capacity.max_total_bytes}",
                limit="max_total_bytes", file_count=len(included),
                total_bytes=total_bytes,
            )
        return CapacityDecision(included, [], False, None)

    selected: list[ScopedEntry] = []
    skipped: list[FileResult] = []
    hit_limit: str | None = None
    bytes_so_far = 0

    def skip(entry: ScopedEntry, detail: str, reason=None) -> None:
        skipped.append(
            FileResult(
                path=entry.relpath,
                disposition=FileDisposition.SKIPPED,
                fail_reason=reason,
                fail_detail=detail,
                size_bytes=entry.size_bytes,
            )
        )

    for index, entry in enumerate(included):
        if index >= capacity.max_files:
            hit_limit = "max_files"
            skip(entry, f"capacity truncation: max_files={capacity.max_files} reached")
            continue
        if hit_limit == "max_total_bytes":
            skip(entry,
                 f"capacity truncation: max_total_bytes={capacity.max_total_bytes} reached")
            continue
        if entry.size_bytes > capacity.max_total_bytes:
            hit_limit = "max_total_bytes"
            skip(entry,
                 f"file size {entry.size_bytes} exceeds max_total_bytes="
                 f"{capacity.max_total_bytes}",
                 reason=FileFailReason.FILE_TOO_LARGE)
            continue
        if bytes_so_far + entry.size_bytes > capacity.max_total_bytes:
            hit_limit = "max_total_bytes"
            skip(entry,
                 f"capacity truncation: max_total_bytes={capacity.max_total_bytes} reached")
            continue
        selected.append(entry)
        bytes_so_far += entry.size_bytes

    return CapacityDecision(selected, skipped, hit_limit is not None, hit_limit)
