"""SQLite-backed durable task store with idempotency-key enforcement."""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time

logger = logging.getLogger(__name__)

from review_service.models import (
    ErrorCategory,
    ReviewRequest,
    ScopeConfig,
    TaskRecord,
    TaskState,
    ensure_transition,
)


class IdempotencyConflictError(Exception):
    """Same idempotency key submitted with different payload."""

    def __init__(self, key: str, existing_task_id: str) -> None:
        self.key = key
        self.existing_task_id = existing_task_id
        super().__init__(
            f"idempotency key {key!r} already used by task {existing_task_id} "
            "with a different payload"
        )


_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    idempotency_key TEXT NOT NULL UNIQUE,
    fingerprint TEXT NOT NULL,
    repo_path TEXT NOT NULL,
    rules TEXT NOT NULL,
    scope TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL,
    error_category TEXT,
    error_detail TEXT,
    findings TEXT NOT NULL DEFAULT '[]',
    file_results TEXT NOT NULL DEFAULT '[]',
    summary TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
"""

#: Columns added after the initial schema; migrated idempotently on open.
_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("scope", "scope TEXT NOT NULL DEFAULT '{}'"),
    ("file_results", "file_results TEXT NOT NULL DEFAULT '[]'"),
    ("summary", "summary TEXT NOT NULL DEFAULT '{}'"),
)


class TaskStore:
    """Durable store; single process, serialized writers, safe across restarts."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        """Add columns introduced after the initial schema, idempotently."""
        existing = {
            row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")
        }
        for name, ddl in _MIGRATIONS:
            if name not in existing:
                self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {ddl}")
        self._conn.commit()

    # -- row mapping ------------------------------------------------------

    def _to_record(self, row: sqlite3.Row) -> TaskRecord:
        return TaskRecord(
            task_id=row["task_id"],
            idempotency_key=row["idempotency_key"],
            request=ReviewRequest(
                repo_path=row["repo_path"],
                rules=tuple(json.loads(row["rules"])),
                scope=ScopeConfig.from_dict(json.loads(row["scope"])),
            ),
            state=TaskState(row["state"]),
            error_category=(
                ErrorCategory(row["error_category"]) if row["error_category"] else None
            ),
            error_detail=row["error_detail"],
            findings=json.loads(row["findings"]),
            file_results=json.loads(row["file_results"]),
            summary=json.loads(row["summary"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    # -- writes -----------------------------------------------------------

    def insert_pending(self, record: TaskRecord) -> TaskRecord:
        """Insert a new PENDING task or return the existing one for the key.

        Raises IdempotencyConflictError if the key exists with a different
        payload fingerprint.
        """
        with self._lock:
            existing = self.get_by_key(record.idempotency_key)
            if existing is not None:
                if existing.request.fingerprint() != record.request.fingerprint():
                    raise IdempotencyConflictError(
                        record.idempotency_key, existing.task_id
                    )
                return existing
            now = time.time()
            record.created_at = record.updated_at = now
            self._conn.execute(
                "INSERT INTO tasks (task_id, idempotency_key, fingerprint, repo_path,"
                " rules, scope, state, error_category, error_detail, findings,"
                " file_results, summary, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record.task_id,
                    record.idempotency_key,
                    record.request.fingerprint(),
                    record.request.repo_path,
                    json.dumps(sorted(record.request.rules)),
                    json.dumps(record.request.scope.to_dict()),
                    record.state.value,
                    None,
                    None,
                    "[]",
                    "[]",
                    "{}",
                    record.created_at,
                    record.updated_at,
                ),
            )
            self._conn.commit()
            return record

    def transition(self, task_id: str, target: TaskState, **fields: object) -> TaskRecord:
        """Atomically move a task to a new state, validating the transition."""
        with self._lock:
            record = self.get(task_id)
            if record is None:
                raise KeyError(f"unknown task {task_id}")
            ensure_transition(record.state, target)
            logger.info(
                "task_id=%s idempotency_key=%s transition %s -> %s",
                record.task_id, record.idempotency_key, record.state.value, target.value,
            )
            record.state = target
            record.updated_at = time.time()
            if "error_category" in fields:
                record.error_category = fields["error_category"]  # type: ignore[assignment]
            if "error_detail" in fields:
                record.error_detail = fields["error_detail"]  # type: ignore[assignment]
            if record.error_category is not None:
                logger.info(
                    "task_id=%s idempotency_key=%s error_category=%s detail=%s",
                    record.task_id, record.idempotency_key,
                    record.error_category.value, record.error_detail,
                )
            if "findings" in fields:
                record.findings = list(fields["findings"])  # type: ignore[arg-type]
            if "file_results" in fields:
                record.file_results = list(fields["file_results"])  # type: ignore[arg-type]
            if "summary" in fields:
                record.summary = dict(fields["summary"])  # type: ignore[arg-type]
            self._conn.execute(
                "UPDATE tasks SET state=?, error_category=?, error_detail=?,"
                " findings=?, file_results=?, summary=?, updated_at=?"
                " WHERE task_id=?",
                (
                    record.state.value,
                    record.error_category.value if record.error_category else None,
                    record.error_detail,
                    json.dumps(record.findings),
                    json.dumps(record.file_results),
                    json.dumps(record.summary),
                    record.updated_at,
                    task_id,
                ),
            )
            self._conn.commit()
            return record

    def reset_inflight(self) -> list[TaskRecord]:
        """On restart: move QUEUED/RUNNING tasks back to QUEUED (recoverable).

        Findings are cleared so a re-run never double-counts partial results
        from the interrupted attempt.
        """
        with self._lock:
            rows = self._conn.execute(
                "SELECT task_id FROM tasks WHERE state IN (?, ?)",
                (TaskState.QUEUED.value, TaskState.RUNNING.value),
            ).fetchall()
            recovered: list[TaskRecord] = []
            for row in rows:
                rec = self.get(row["task_id"])
                assert rec is not None
                # Bypass the state machine: recovery is a system-level reset.
                rec.state = TaskState.QUEUED
                rec.findings = []
                rec.file_results = []
                rec.summary = {}
                rec.error_category = None
                rec.error_detail = None
                rec.updated_at = time.time()
                self._conn.execute(
                    "UPDATE tasks SET state=?, findings='[]', file_results='[]',"
                    " summary='{}', error_category=NULL,"
                    " error_detail=NULL, updated_at=? WHERE task_id=?",
                    (rec.state.value, rec.updated_at, rec.task_id),
                )
                recovered.append(rec)
            self._conn.commit()
            return recovered

    # -- reads ------------------------------------------------------------

    def get(self, task_id: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE task_id=?", (task_id,)
        ).fetchone()
        return self._to_record(row) if row else None

    def get_by_key(self, idempotency_key: str) -> TaskRecord | None:
        row = self._conn.execute(
            "SELECT * FROM tasks WHERE idempotency_key=?", (idempotency_key,)
        ).fetchone()
        return self._to_record(row) if row else None

    def list_all(self) -> list[TaskRecord]:
        rows = self._conn.execute("SELECT * FROM tasks ORDER BY created_at").fetchall()
        return [self._to_record(r) for r in rows]

    def close(self) -> None:
        self._conn.close()
