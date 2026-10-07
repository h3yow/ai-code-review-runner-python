"""Core type definitions: task state machine, error categories, task records."""

from __future__ import annotations

import enum
import json
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
    """Distinguishable failure categories for review tasks."""

    INPUT_INVALID = "INPUT_INVALID"        # malformed request / unreadable repo path
    UNSUPPORTED_INPUT = "UNSUPPORTED_INPUT"  # well-formed but unsupported content
    PARSE_ERROR = "PARSE_ERROR"            # source parsing stage failed
    INDEX_ERROR = "INDEX_ERROR"            # symbol indexing stage failed
    RULE_ERROR = "RULE_ERROR"              # rule execution stage failed
    INTERRUPTED = "INTERRUPTED"            # service restarted mid-flight
    CAPACITY = "CAPACITY"                  # rejected: concurrency/queue limit reached
    REPO_TOO_LARGE = "REPO_TOO_LARGE"      # rejected: in-scope repo exceeds size limits
    INTERNAL = "INTERNAL"                  # unexpected internal failure


class OverflowPolicy(str, enum.Enum):
    """How to behave when an in-scope repository exceeds the capacity limits."""

    REJECT = "reject"      # reject the whole task deterministically (REPO_TOO_LARGE)
    TRUNCATE = "truncate"  # process a documented stable prefix; mark the rest


@dataclass(frozen=True)
class ScopeConfig:
    """User-configurable review scope.

    All patterns are matched against repo-relative POSIX-style paths.
    Empty include lists mean "everything not excluded".
    """

    recursive: bool = True
    include_dirs: tuple[str, ...] = ()
    include_names: tuple[str, ...] = ()
    include_suffixes: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()


@dataclass(frozen=True)
class CapacityConfig:
    """Configurable upper bounds for one task."""

    max_files: int = 10_000
    max_total_bytes: int = 10 * 1024 * 1024  # 10 MiB
    overflow_policy: OverflowPolicy = OverflowPolicy.REJECT


class FileDisposition(str, enum.Enum):
    """Where an in-scope file ended up."""

    REVIEWED = "REVIEWED"   # actually reviewed; carries its own findings
    EXCLUDED = "EXCLUDED"   # blocked by a user exclude rule
    FAILED = "FAILED"       # could not be processed; carries a FileFailReason
    SKIPPED = "SKIPPED"     # deterministically not processed (capacity truncation)


class FileFailReason(str, enum.Enum):
    """Distinguishable per-file failure reasons."""

    NOT_TEXT = "NOT_TEXT"                 # bytes cannot be decoded as UTF-8 text
    UNSUPPORTED_FILE_TYPE = "UNSUPPORTED_FILE_TYPE"  # text, but no supported source type
    READ_ERROR = "READ_ERROR"             # OS-level read failure
    FILE_TOO_LARGE = "FILE_TOO_LARGE"     # single file exceeds max_total_bytes
    PARSE_ERROR = "PARSE_ERROR"           # source parsing failed (e.g. syntax error)
    INDEX_ERROR = "INDEX_ERROR"           # symbol indexing failed
    RULE_ERROR = "RULE_ERROR"             # rule execution failed


@dataclass
class FileResult:
    """The outcome for one in-scope file. Nothing in scope disappears."""

    path: str                       # repo-relative POSIX-style path
    disposition: FileDisposition
    findings: list[dict] = field(default_factory=list)
    fail_reason: FileFailReason | None = None
    fail_detail: str | None = None
    excluded_by: str | None = None  # the user exclude pattern that blocked it
    size_bytes: int = 0


@dataclass
class ScopeSummary:
    """Aggregate, at-a-glance counts over every file that entered scope."""

    total_in_scope: int = 0
    reviewed: int = 0
    excluded: int = 0
    failed: int = 0
    skipped: int = 0
    truncated: bool = False
    rejected_by_limit: str | None = None  # which limit rejected/truncated

    @property
    def has_skips_or_failures(self) -> bool:
        return bool(self.failed or self.skipped or self.truncated)


@dataclass(frozen=True)
class ReviewRequest:
    """Payload describing what to review."""

    repo_path: str
    rules: tuple[str, ...] = ()
    scope: ScopeConfig = field(default_factory=ScopeConfig)
    capacity: CapacityConfig = field(default_factory=CapacityConfig)

    def fingerprint(self) -> str:
        """Stable content hash used for idempotency conflict detection."""
        import hashlib

        blob = json.dumps(
            {
                "repo_path": self.repo_path,
                "rules": sorted(self.rules),
                "scope": {
                    "recursive": self.scope.recursive,
                    "include_dirs": sorted(self.scope.include_dirs),
                    "include_names": sorted(self.scope.include_names),
                    "include_suffixes": sorted(self.scope.include_suffixes),
                    "exclude_patterns": list(self.scope.exclude_patterns),
                },
                "capacity": {
                    "max_files": self.capacity.max_files,
                    "max_total_bytes": self.capacity.max_total_bytes,
                    "overflow_policy": self.capacity.overflow_policy.value,
                },
            },
            sort_keys=True,
        )
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
    file_results: list[FileResult] = field(default_factory=list)
    summary: ScopeSummary | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
