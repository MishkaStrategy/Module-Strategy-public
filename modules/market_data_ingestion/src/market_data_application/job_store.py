from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

from pydantic import BaseModel

from market_data_ingestion.enums import RunState
from market_data_ingestion.state_machine import IngestionStateMachine

from .contracts import (
    JobCreatedResponse,
    JobDetails,
    JobError,
    JobEvent,
    JobEventType,
    JobListResponse,
    JobOperation,
    JobProgress,
    JobSummary,
    Pagination,
    canonical_request_hash,
    canonical_request_payload,
)
from .errors import (
    IdempotencyConflictError,
    InvalidJobTransitionError,
    JobNotFoundError,
    RepairConfirmationNotFoundError,
    RepairConfirmationRejectedError,
)
from .repair_execution_contracts import RepairJobRequest

_TERMINAL = frozenset(
    {
        RunState.COMPLETED,
        RunState.COMPLETED_WITH_WARNINGS,
        RunState.CANCELLED,
        RunState.FAILED,
        RunState.DATASET_REJECTED,
    }
)


class SQLiteJobStore:
    """Durable operational jobs, intentionally separate from dataset registration."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connection(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connection(write=True) as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    operation TEXT NOT NULL,
                    state TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    backend_run_id TEXT,
                    progress_json TEXT,
                    result_json TEXT,
                    error_json TEXT,
                    warnings_json TEXT NOT NULL,
                    correlation_id TEXT NOT NULL,
                    actor_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1)
                );
                CREATE INDEX IF NOT EXISTS idx_jobs_state_updated
                    ON jobs(state, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_operation_updated
                    ON jobs(operation, updated_at DESC);
                CREATE TABLE IF NOT EXISTS job_events (
                    job_id TEXT NOT NULL,
                    event_id INTEGER NOT NULL CHECK(event_id > 0),
                    event_type TEXT NOT NULL,
                    state TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    PRIMARY KEY(job_id, event_id),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                """
            )

    def create_job(
        self,
        *,
        operation: JobOperation,
        request: BaseModel | Mapping[str, Any],
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> JobCreatedResponse:
        key = self._identifier(idempotency_key, "idempotency_key", 255)
        correlation = self._identifier(
            correlation_id or f"corr_{uuid4().hex}", "correlation_id", 128
        )
        actor = self._identifier(actor_id, "actor_id", 128) if actor_id else None
        request_payload = canonical_request_payload(request)
        request_hash = canonical_request_hash(request_payload)
        now = datetime.now(UTC)
        reused_id: str | None = None
        created_id: str | None = None
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT job_id, operation, request_hash FROM jobs WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing:
                if (
                    existing["operation"] != operation.value
                    or existing["request_hash"] != request_hash
                ):
                    raise IdempotencyConflictError(
                        idempotency_key=key, job_id=str(existing["job_id"])
                    )
                reused_id = str(existing["job_id"])
            else:
                created_id = f"mdi_job_{uuid4().hex[:20]}"
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, operation, state, request_json, request_hash,
                        idempotency_key, warnings_json, correlation_id, actor_id,
                        created_at, updated_at, schema_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
                    """,
                    (
                        created_id,
                        operation.value,
                        RunState.CREATED.value,
                        self._json(request_payload),
                        request_hash,
                        key,
                        "[]",
                        correlation,
                        actor,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._append_event(
                    connection,
                    created_id,
                    JobEventType.STATE,
                    RunState.CREATED,
                    now,
                    {"operation": operation.value, "request_hash": request_hash},
                )
        job_id = reused_id or created_id
        if job_id is None:
            raise RuntimeError("job identity was not created")
        return JobCreatedResponse(job=self.get_job(job_id), reused=reused_id is not None)

    def create_repair_job(
        self,
        *,
        request: RepairJobRequest,
        confirmation_token: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str,
    ) -> JobCreatedResponse:
        key = self._identifier(idempotency_key, "idempotency_key", 255)
        correlation = self._identifier(
            correlation_id or f"corr_{uuid4().hex}", "correlation_id", 128
        )
        actor = self._identifier(actor_id, "actor_id", 128)
        request_payload = canonical_request_payload(request)
        request_hash = canonical_request_hash(request_payload)
        token_digest = hashlib.sha256(confirmation_token.encode()).hexdigest()
        now = datetime.now(UTC)
        reused_id: str | None = None
        created_id: str | None = None
        with self._connection(write=True) as connection:
            existing = connection.execute(
                "SELECT job_id, operation, request_hash FROM jobs WHERE idempotency_key=?",
                (key,),
            ).fetchone()
            if existing:
                if (
                    existing["operation"] != JobOperation.REPAIR.value
                    or existing["request_hash"] != request_hash
                ):
                    raise IdempotencyConflictError(
                        idempotency_key=key, job_id=str(existing["job_id"])
                    )
                reused_id = str(existing["job_id"])
                confirmation = self._repair_confirmation_row(connection, request.confirmation_id)
                self._validate_repair_confirmation(
                    confirmation,
                    request=request,
                    actor_id=actor,
                    token_digest=token_digest,
                    at=now,
                    expected_job_id=reused_id,
                )
            else:
                confirmation = self._repair_confirmation_row(connection, request.confirmation_id)
                self._validate_repair_confirmation(
                    confirmation,
                    request=request,
                    actor_id=actor,
                    token_digest=token_digest,
                    at=now,
                    expected_job_id=None,
                )
                created_id = f"mdi_job_{uuid4().hex[:20]}"
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, operation, state, request_json, request_hash,
                        idempotency_key, warnings_json, correlation_id, actor_id,
                        created_at, updated_at, schema_version
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,1)
                    """,
                    (
                        created_id,
                        JobOperation.REPAIR.value,
                        RunState.CREATED.value,
                        self._json(request_payload),
                        request_hash,
                        key,
                        "[]",
                        correlation,
                        actor,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                reserved = connection.execute(
                    """
                    UPDATE repair_confirmations
                    SET reserved_job_id=?, reserved_at=?
                    WHERE confirmation_id=? AND reserved_job_id IS NULL
                      AND consumed_at IS NULL AND revoked_at IS NULL AND expires_at>?
                    """,
                    (
                        created_id,
                        now.isoformat(),
                        request.confirmation_id,
                        now.isoformat(),
                    ),
                )
                if reserved.rowcount != 1:
                    raise RepairConfirmationRejectedError(
                        confirmation_id=request.confirmation_id,
                        reason="concurrent_reservation",
                    )
                self._append_event(
                    connection,
                    created_id,
                    JobEventType.STATE,
                    RunState.CREATED,
                    now,
                    {
                        "operation": JobOperation.REPAIR.value,
                        "request_hash": request_hash,
                        "confirmation_id": request.confirmation_id,
                    },
                )
        job_id = reused_id or created_id
        if job_id is None:
            raise RuntimeError("repair job identity was not created")
        return JobCreatedResponse(job=self.get_job(job_id), reused=reused_id is not None)

    @staticmethod
    def _repair_confirmation_row(
        connection: sqlite3.Connection, confirmation_id: str
    ) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM repair_confirmations WHERE confirmation_id=?",
            (confirmation_id,),
        ).fetchone()
        if row is None:
            raise RepairConfirmationNotFoundError(confirmation_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _validate_repair_confirmation(
        row: sqlite3.Row,
        *,
        request: RepairJobRequest,
        actor_id: str,
        token_digest: str,
        at: datetime,
        expected_job_id: str | None,
    ) -> None:
        reason: str | None = None
        selected = row["selected_data_type"]
        selected_value = str(selected) if selected is not None else None
        if str(row["dataset_id"]) != request.dataset_id:
            reason = "dataset_mismatch"
        elif int(row["version"]) != request.version:
            reason = "version_mismatch"
        elif str(row["preview_id"]) != request.preview_id:
            reason = "preview_mismatch"
        elif str(row["action"]) != request.action:
            reason = "action_mismatch"
        elif selected_value != (
            request.selected_data_type.value if request.selected_data_type is not None else None
        ):
            reason = "data_type_mismatch"
        elif str(row["actor_id"]) != actor_id:
            reason = "actor_mismatch"
        elif not hmac.compare_digest(str(row["token_digest"]), token_digest):
            reason = "invalid_token"
        elif expected_job_id is not None:
            reserved_job_id = (
                str(row["reserved_job_id"]) if row["reserved_job_id"] is not None else None
            )
            consumed_job_id = (
                str(row["consumed_job_id"]) if row["consumed_job_id"] is not None else None
            )
            if expected_job_id not in {reserved_job_id, consumed_job_id}:
                reason = "job_reservation_mismatch"
        elif row["revoked_at"] is not None:
            reason = "revoked"
        elif row["consumed_at"] is not None:
            reason = "already_consumed"
        elif datetime.fromisoformat(str(row["expires_at"])) <= at:
            reason = "expired"
        elif row["reserved_job_id"] is not None:
            reason = "already_reserved"
        if reason is not None:
            raise RepairConfirmationRejectedError(
                confirmation_id=request.confirmation_id,
                reason=reason,
            )

    def get_job(self, job_id: str) -> JobDetails:
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError(job_id)
        return self._details(row)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        states: Sequence[RunState] = (),
        operations: Sequence[JobOperation] = (),
    ) -> JobListResponse:
        if not 1 <= limit <= 200 or offset < 0:
            raise ValueError("invalid pagination")
        clauses: list[str] = []
        params: list[object] = []
        for column, values in (("state", states), ("operation", operations)):
            if values:
                clauses.append(f"{column} IN ({','.join('?' for _ in values)})")
                params.extend(item.value for item in values)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with self._connection() as connection:
            total = int(
                connection.execute("SELECT COUNT(*) FROM jobs" + where, params).fetchone()[0]
            )
            rows = connection.execute(
                "SELECT * FROM jobs"
                + where
                + " ORDER BY created_at DESC, job_id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset],
            ).fetchall()
        return JobListResponse(
            items=tuple(self._summary(row) for row in rows),
            pagination=Pagination(limit=limit, offset=offset, total=total),
        )

    def transition(
        self,
        job_id: str,
        target: RunState,
        *,
        progress: JobProgress | None = None,
        backend_run_id: str | None = None,
        result: Mapping[str, Any] | None = None,
        error: JobError | None = None,
        warnings: Sequence[str] = (),
    ) -> JobDetails:
        now = datetime.now(UTC)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            current = RunState(str(row["state"]))
            if current != target:
                try:
                    IngestionStateMachine(state=current).transition(target)
                except Exception as exc:
                    raise InvalidJobTransitionError(
                        job_id=job_id, state=current.value, target=target.value
                    ) from exc
            if target in {RunState.FAILED, RunState.DATASET_REJECTED} and error is None:
                raise ValueError(f"{target.value} transition requires error")
            started = row["started_at"] or (
                now.isoformat() if target is not RunState.CREATED else None
            )
            completed = now.isoformat() if target in _TERMINAL else row["completed_at"]
            merged_warnings = tuple(
                dict.fromkeys(
                    [
                        *json.loads(row["warnings_json"]),
                        *(item.strip() for item in warnings if item.strip()),
                    ]
                )
            )
            values = {
                "backend_run_id": backend_run_id or row["backend_run_id"],
                "progress_json": self._model_json(progress) or row["progress_json"],
                "result_json": (
                    self._json(dict(result)) if result is not None else row["result_json"]
                ),
                "error_json": self._model_json(error) or row["error_json"],
            }
            connection.execute(
                """
                UPDATE jobs SET state=?, backend_run_id=?, progress_json=?, result_json=?,
                    error_json=?, warnings_json=?, updated_at=?, started_at=?, completed_at=?
                WHERE job_id=?
                """,
                (
                    target.value,
                    values["backend_run_id"],
                    values["progress_json"],
                    values["result_json"],
                    values["error_json"],
                    self._json(merged_warnings),
                    now.isoformat(),
                    started,
                    completed,
                    job_id,
                ),
            )
            payload = {
                key: value
                for key, value in {
                    "progress": progress.model_dump(mode="json") if progress else None,
                    "result": dict(result) if result is not None else None,
                    "error": error.model_dump(mode="json") if error else None,
                    "warnings": list(merged_warnings) if merged_warnings else None,
                    "backend_run_id": values["backend_run_id"],
                }.items()
                if value is not None
            }
            event_type = JobEventType.STATE
            if target in {RunState.FAILED, RunState.DATASET_REJECTED}:
                event_type = JobEventType.ERROR
            elif target in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}:
                event_type = JobEventType.RESULT
            self._append_event(connection, job_id, event_type, target, now, payload)
        return self.get_job(job_id)

    def record_progress(self, job_id: str, progress: JobProgress) -> JobDetails:
        now = datetime.now(UTC)
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError(job_id)
            state = RunState(str(row["state"]))
            if state in _TERMINAL:
                raise InvalidJobTransitionError(
                    job_id=job_id, state=state.value, target=progress.phase.value
                )
            if progress.phase is not state:
                raise ValueError("progress phase must match current job state")
            connection.execute(
                "UPDATE jobs SET progress_json=?, updated_at=? WHERE job_id=?",
                (self._model_json(progress), now.isoformat(), job_id),
            )
            self._append_event(
                connection,
                job_id,
                JobEventType.PROGRESS,
                state,
                now,
                {"progress": progress.model_dump(mode="json")},
            )
        return self.get_job(job_id)

    def events_after(
        self, job_id: str, *, after_event_id: int = 0, limit: int = 500
    ) -> tuple[JobEvent, ...]:
        if after_event_id < 0 or not 1 <= limit <= 1000:
            raise ValueError("invalid event pagination")
        with self._connection() as connection:
            if (
                connection.execute("SELECT 1 FROM jobs WHERE job_id=?", (job_id,)).fetchone()
                is None
            ):
                raise JobNotFoundError(job_id)
            rows = connection.execute(
                """
                SELECT event_id, event_type, state, timestamp, payload_json
                FROM job_events WHERE job_id=? AND event_id>?
                ORDER BY event_id ASC LIMIT ?
                """,
                (job_id, after_event_id, limit),
            ).fetchall()
        return tuple(
            JobEvent(
                event_id=int(row["event_id"]),
                job_id=job_id,
                event_type=JobEventType(str(row["event_type"])),
                state=RunState(str(row["state"])),
                timestamp=self._datetime(row["timestamp"]),
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )

    def recoverable_jobs(self, *, limit: int = 200) -> tuple[JobDetails, ...]:
        if not 1 <= limit <= 1000:
            raise ValueError("invalid recovery limit")
        states = sorted(_TERMINAL, key=lambda item: item.value)
        placeholders = ",".join("?" for _ in states)
        with self._connection() as connection:
            rows = connection.execute(
                f"SELECT * FROM jobs WHERE state NOT IN ({placeholders}) "
                "ORDER BY updated_at ASC LIMIT ?",
                [*(item.value for item in states), limit],
            ).fetchall()
        return tuple(self._details(row) for row in rows)

    def _append_event(
        self,
        connection: sqlite3.Connection,
        job_id: str,
        event_type: JobEventType,
        state: RunState,
        timestamp: datetime,
        payload: Mapping[str, Any],
    ) -> None:
        event_id = int(
            connection.execute(
                "SELECT COALESCE(MAX(event_id), 0) + 1 FROM job_events WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
        )
        connection.execute(
            """
            INSERT INTO job_events(
                job_id, event_id, event_type, state, timestamp, payload_json, schema_version
            ) VALUES(?,?,?,?,?,?,1)
            """,
            (
                job_id,
                event_id,
                event_type.value,
                state.value,
                timestamp.isoformat(),
                self._json(dict(payload)),
            ),
        )

    def _summary(self, row: sqlite3.Row) -> JobSummary:
        return JobSummary(
            job_id=str(row["job_id"]),
            operation=JobOperation(str(row["operation"])),
            state=RunState(str(row["state"])),
            request_hash=str(row["request_hash"]),
            backend_run_id=row["backend_run_id"],
            progress=(
                JobProgress.model_validate(json.loads(row["progress_json"]))
                if row["progress_json"]
                else None
            ),
            created_at=self._datetime(row["created_at"]),
            updated_at=self._datetime(row["updated_at"]),
            started_at=self._optional_datetime(row["started_at"]),
            completed_at=self._optional_datetime(row["completed_at"]),
        )

    def _details(self, row: sqlite3.Row) -> JobDetails:
        summary = self._summary(row)
        return JobDetails(
            **summary.model_dump(),
            request=json.loads(row["request_json"]),
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=(
                JobError.model_validate(json.loads(row["error_json"]))
                if row["error_json"]
                else None
            ),
            warnings=tuple(json.loads(row["warnings_json"])),
            correlation_id=str(row["correlation_id"]),
            actor_id=row["actor_id"],
        )

    @staticmethod
    def _identifier(value: str, name: str, maximum: int) -> str:
        if not isinstance(value, str):
            raise TypeError(f"{name} must be a string")
        normalized = value.strip()
        if not normalized or len(normalized) > maximum:
            raise ValueError(f"{name} must contain between 1 and {maximum} characters")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError(f"{name} must not contain control characters")
        return normalized

    @staticmethod
    def _json(value: Any) -> str:
        return json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
            default=str,
        )

    @classmethod
    def _model_json(cls, value: BaseModel | None) -> str | None:
        return None if value is None else cls._json(value.model_dump(mode="json"))

    @staticmethod
    def _datetime(value: str) -> datetime:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("stored datetime must be timezone-aware")
        return parsed.astimezone(UTC)

    @classmethod
    def _optional_datetime(cls, value: str | None) -> datetime | None:
        return None if value is None else cls._datetime(value)
