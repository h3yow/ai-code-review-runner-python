"""Structured-ish logging helpers used across the runner.

All task lifecycle log lines carry the task id, idempotency key, the state
transition and (when relevant) an error category, so that unit-test logs are
self-explanatory.
"""
from __future__ import annotations

import logging
import sys

_DEFAULT_FORMAT = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
_configured = False


def configure_logging(level: int = logging.INFO) -> None:
    """Configure root logging once; repeated calls only adjust the level."""
    global _configured
    root = logging.getLogger()
    if not _configured:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter(_DEFAULT_FORMAT))
        root.addHandler(handler)
        _configured = True
    root.setLevel(level)


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)


def log_transition(
    logger: logging.Logger,
    task_id: str,
    idempotency_key: str,
    from_state: str,
    to_state: str,
    *,
    error_category: str | None = None,
    note: str | None = None,
) -> None:
    """Emit one deterministic lifecycle log line."""
    parts = [
        f"task_id={task_id}",
        f"idempotency_key={idempotency_key}",
        f"transition={from_state}->{to_state}",
    ]
    if error_category is not None:
        parts.append(f"error_category={error_category}")
    if note:
        parts.append(f"note={note}")
    logger.info("TASK %s", " ".join(parts))
