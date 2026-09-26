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
    """Distinguishable failure categories for review tasks."""

    INPUT_INVALID = "INPUT_INVALID"        # malformed request / unreadable repo path
    UNSUPPORTED_INPUT = "UNSUPPORTED_INPUT"  # well-formed but unsupported content
    PARSE_ERROR = "PARSE_ERROR"            # source parsing stage failed
    INDEX_ERROR = "INDEX_ERROR"            # symbol indexing stage failed
    RULE_ERROR = "RULE_ERROR"              # rule execution stage failed
    INTERRUPTED = "INTERRUPTED"            # service restarted mid-flight
    CAPACITY = "CAPACITY"                  # rejected: concurrency/queue limit reached
    INTERNAL = "INTERNAL"                  # unexpected internal failure


@dataclass(frozen=True)
class ReviewRequest:
    """Payload describing what to review."""

    repo_path: str
    rules: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        """Stable content hash used for idempotency conflict detection."""
        import hashlib
        import json

        blob = json.dumps({"repo_path": self.repo_path, "rules": sorted(self.rules)})
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
    created_at: float = 0.0
    updated_at: float = 0.0
