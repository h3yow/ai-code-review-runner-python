"""Core type definitions: task state machine, error categories, task records."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class TaskState(str, enum.Enum):
    """Lifecycle states of a review task."""

    PENDING = "PENDING"
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELED = "CANCELED"


#: Legal state transitions. Terminal states have no outgoing edges.
ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset({TaskState.QUEUED, TaskState.CANCELED}),
    TaskState.QUEUED: frozenset({TaskState.RUNNING, TaskState.FAILED, TaskState.CANCELED}),
    TaskState.RUNNING: frozenset({TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELED: frozenset(),
}

TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELED}
)


class InvalidTransitionError(Exception):
    """Raised when a state transition is not allowed by the state machine."""


def ensure_transition(current: TaskState, target: TaskState) -> None:
    """Validate a transition, raising InvalidTransitionError if illegal."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise InvalidTransitionError(f"{current.value} -> {target.value} is not allowed")


class ErrorCategory(str, enum.Enum):
    """Distinguishable failure categories for review tasks and files."""

    INPUT_INVALID = "INPUT_INVALID"        # malformed request / unreadable path
    UNSUPPORTED_INPUT = "UNSUPPORTED_INPUT"  # well-formed but unsupported content
    NOT_TEXT = "NOT_TEXT"                  # file bytes cannot be decoded as text
    PARSE_ERROR = "PARSE_ERROR"            # source parsing stage failed
    INDEX_ERROR = "INDEX_ERROR"            # symbol indexing stage failed
    RULE_ERROR = "RULE_ERROR"              # rule execution stage failed
    INTERRUPTED = "INTERRUPTED"            # service restarted mid-flight
    CAPACITY = "CAPACITY"                  # rejected: concurrency/queue limit reached
    SCOPE_LIMIT_EXCEEDED = "SCOPE_LIMIT_EXCEEDED"  # repo exceeds configured scope limits
    INTERNAL = "INTERNAL"                  # unexpected internal failure


class FileStatus(str, enum.Enum):
    """Per-file outcome within a review task."""

    REVIEWED = "REVIEWED"    # file was reviewed; carries its own findings
    EXCLUDED = "EXCLUDED"    # knocked out by a user-supplied exclude rule
    FAILED = "FAILED"        # processing failed; carries an ErrorCategory


@dataclass(frozen=True)
class ScopeConfig:
    """User-configurable review scope.

    - include_dirs: repo-relative subdirectories to scan; empty = repo root.
    - include_names: fnmatch patterns on the file name; empty = all names.
    - include_extensions: suffixes like ".py"; empty = all extensions.
    - exclude: fnmatch patterns on the repo-relative POSIX path.
    - recursive: whether to descend into subdirectories of the include dirs.
    """

    include_dirs: tuple[str, ...] = ()
    include_names: tuple[str, ...] = ()
    include_extensions: tuple[str, ...] = (".py",)
    exclude: tuple[str, ...] = ()
    recursive: bool = True

    def to_dict(self) -> dict:
        return {
            "include_dirs": sorted(self.include_dirs),
            "include_names": sorted(self.include_names),
            "include_extensions": sorted(self.include_extensions),
            "exclude": sorted(self.exclude),
            "recursive": self.recursive,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> ScopeConfig:
        if not data:
            return cls()
        return cls(
            include_dirs=tuple(data.get("include_dirs") or ()),
            include_names=tuple(data.get("include_names") or ()),
            include_extensions=tuple(
                data.get("include_extensions")
                if data.get("include_extensions") is not None
                else (".py",)
            ),
            exclude=tuple(data.get("exclude") or ()),
            recursive=bool(data.get("recursive", True)),
        )


class LimitPolicy(str, enum.Enum):
    """How to react when a repository exceeds the configured scope limits."""

    REJECT = "REJECT"      # fail the task with SCOPE_LIMIT_EXCEEDED
    TRUNCATE = "TRUNCATE"  # keep a deterministic prefix, mark the summary


@dataclass(frozen=True)
class ReviewLimits:
    """Service-level capacity limits for a single review task."""

    max_files: int = 5000
    max_total_bytes: int = 50 * 1024 * 1024
    policy: LimitPolicy = LimitPolicy.REJECT


@dataclass(frozen=True)
class ReviewRequest:
    """Payload describing what to review."""

    repo_path: str
    rules: tuple[str, ...] = ()
    scope: ScopeConfig = ScopeConfig()

    def fingerprint(self) -> str:
        """Stable content hash used for idempotency conflict detection."""
        import hashlib
        import json

        blob = json.dumps({
            "repo_path": self.repo_path,
            "rules": sorted(self.rules),
            "scope": self.scope.to_dict(),
        })
        return hashlib.sha256(blob.encode()).hexdigest()


@dataclass
class TaskRecord:
    """Persisted task row."""

    task_id: str
    idempotency_key: str
    request: ReviewRequest
    state: TaskState = TaskState.PENDING
    error_category: ErrorCategory | None = None
    error_detail: str | None = None
    findings: list[dict] = field(default_factory=list)
    file_results: list[dict] = field(default_factory=list)
    summary: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0
