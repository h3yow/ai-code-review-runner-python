"""Domain models: task states, requests, results."""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from review_runner.errors import ErrorCategory, ReviewError


class TaskState(str, Enum):
    """Task lifecycle states.

    PENDING -> RUNNING -> SUCCEEDED | FAILED
    PENDING/RUNNING -> CANCELLING -> CANCELLED
    There are no other legal transitions (no state may be skipped).
    """

    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLING = "CANCELLING"
    CANCELLED = "CANCELLED"


TERMINAL_STATES: frozenset[TaskState] = frozenset(
    {TaskState.SUCCEEDED, TaskState.FAILED, TaskState.CANCELLED}
)

ALLOWED_TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.PENDING: frozenset(
        {TaskState.RUNNING, TaskState.CANCELLING, TaskState.FAILED}
    ),
    TaskState.RUNNING: frozenset(
        {
            TaskState.SUCCEEDED,
            TaskState.FAILED,
            TaskState.CANCELLING,
        }
    ),
    TaskState.CANCELLING: frozenset({TaskState.CANCELLED, TaskState.FAILED}),
    TaskState.SUCCEEDED: frozenset(),
    TaskState.FAILED: frozenset(),
    TaskState.CANCELLED: frozenset(),
}


def new_task_id() -> str:
    return f"task-{uuid.uuid4().hex[:16]}"


def validate_transition(current: TaskState, target: TaskState) -> None:
    """Reject any state change that would jump over the state machine."""
    if target not in ALLOWED_TRANSITIONS[current]:
        raise RuntimeError(
            f"illegal state transition: {current.value} -> {target.value}"
        )


@dataclass(frozen=True)
class ReviewRequest:
    repo_path: str
    idempotency_key: str
    rules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.idempotency_key, str) or not self.idempotency_key.strip():
            raise ReviewError(
                ErrorCategory.VALIDATION_ERROR, "idempotency_key must be a non-empty string"
            )
        if not isinstance(self.repo_path, str) or not self.repo_path.strip():
            raise ReviewError(
                ErrorCategory.VALIDATION_ERROR, "repo_path must be a non-empty string"
            )
        if not all(isinstance(r, str) and r for r in self.rules):
            raise ReviewError(
                ErrorCategory.VALIDATION_ERROR, "rules must be non-empty strings"
            )
        object.__setattr__(self, "repo_path", self.repo_path.strip())
        object.__setattr__(self, "idempotency_key", self.idempotency_key.strip())
        object.__setattr__(
            self, "rules", tuple(sorted(r.strip() for r in self.rules))
        )

    def fingerprint(self) -> str:
        """Content hash: same key with a different fingerprint is a conflict."""
        payload = json.dumps(
            {
                "repo_path": str(Path(self.repo_path).resolve())
                if Path(self.repo_path).exists()
                else self.repo_path,
                "rules": list(self.rules),
            },
            sort_keys=True,
        )
        return uuid.uuid5(uuid.NAMESPACE_URL, f"review-request:{payload}").hex


@dataclass
class ReviewFinding:
    rule_id: str
    file_path: str
    line: int
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "file_path": self.file_path,
            "line": self.line,
            "message": self.message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewFinding":
        return cls(
            rule_id=data["rule_id"],
            file_path=data["file_path"],
            line=int(data["line"]),
            message=data["message"],
        )


@dataclass
class ReviewResult:
    findings: list[ReviewFinding] = field(default_factory=list)
    files_scanned: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "findings": [f.to_dict() for f in self.findings],
            "files_scanned": self.files_scanned,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ReviewResult":
        return cls(
            findings=[ReviewFinding.from_dict(f) for f in data.get("findings", [])],
            files_scanned=int(data.get("files_scanned", 0)),
        )


@dataclass
class Task:
    task_id: str
    idempotency_key: str
    request_fingerprint: str
    request: ReviewRequest
    state: TaskState = TaskState.PENDING
    result: ReviewResult | None = None
    error_category: str | None = None
    error_message: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0
    version: int = 0

    @classmethod
    def create(cls, request: ReviewRequest) -> "Task":
        now = time.time()
        return cls(
            task_id=new_task_id(),
            idempotency_key=request.idempotency_key,
            request_fingerprint=request.fingerprint(),
            request=request,
            state=TaskState.PENDING,
            created_at=now,
            updated_at=now,
        )

    def to_record(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.request_fingerprint,
            "request": {
                "repo_path": self.request.repo_path,
                "idempotency_key": self.request.idempotency_key,
                "rules": list(self.request.rules),
            },
            "state": self.state.value,
            "result": self.result.to_dict() if self.result is not None else None,
            "error_category": self.error_category,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "version": self.version,
        }

    @classmethod
    def from_record(cls, record: dict[str, Any]) -> "Task":
        req_data = record["request"]
        request = ReviewRequest(
            repo_path=req_data["repo_path"],
            idempotency_key=req_data["idempotency_key"],
            rules=tuple(req_data.get("rules", ())),
        )
        return cls(
            task_id=record["task_id"],
            idempotency_key=record["idempotency_key"],
            request_fingerprint=record["request_fingerprint"],
            request=request,
            state=TaskState(record["state"]),
            result=ReviewResult.from_dict(record["result"])
            if record.get("result") is not None
            else None,
            error_category=record.get("error_category"),
            error_message=record.get("error_message"),
            created_at=float(record.get("created_at", 0.0)),
            updated_at=float(record.get("updated_at", 0.0)),
            version=int(record.get("version", 0)),
        )
