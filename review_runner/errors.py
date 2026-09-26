"""Error categories and domain exceptions for review tasks."""
from __future__ import annotations

from enum import Enum


class ErrorCategory(str, Enum):
    """Distinct, stable error categories reported to clients.

    - VALIDATION_ERROR:     request fields missing or malformed (empty key/path)
    - REPO_NOT_FOUND:       the repository path does not exist or is not a dir
    - UNSUPPORTED_FILE:     a tracked file has a suffix we do not parse
    - MALFORMED_INPUT:      a file cannot be decoded / is syntactically broken
    - INDEX_ERROR:          building the repository index failed
    - RULE_EXECUTION_ERROR:  a review rule raised while executing
    - IDEMPOTENCY_CONFLICT: same idempotency key, different request content
    - QUEUE_FULL:           concurrency/queue capacity exhausted
    - TASK_NOT_FOUND:       unknown task id or idempotency key
    - CANCELLED:            task finished as the result of a cancellation
    - INTERRUPTED:          task was RUNNING when the process restarted
    - INTERNAL_ERROR:       unexpected defensive failure (should never happen)
    """

    VALIDATION_ERROR = "VALIDATION_ERROR"
    REPO_NOT_FOUND = "REPO_NOT_FOUND"
    UNSUPPORTED_FILE = "UNSUPPORTED_FILE"
    MALFORMED_INPUT = "MALFORMED_INPUT"
    INDEX_ERROR = "INDEX_ERROR"
    RULE_EXECUTION_ERROR = "RULE_EXECUTION_ERROR"
    IDEMPOTENCY_CONFLICT = "IDEMPOTENCY_CONFLICT"
    QUEUE_FULL = "QUEUE_FULL"
    TASK_NOT_FOUND = "TASK_NOT_FOUND"
    CANCELLED = "CANCELLED"
    INTERRUPTED = "INTERRUPTED"
    INTERNAL_ERROR = "INTERNAL_ERROR"


class ReviewError(Exception):
    """Domain error carrying a stable, distinguishable ErrorCategory."""

    def __init__(self, category: ErrorCategory, message: str) -> None:
        super().__init__(message)
        self._category = category
        self.message = message

    @property
    def category(self) -> ErrorCategory:
        return self._category

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"[{self._category.value}] {self.message}"
