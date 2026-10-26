from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .contracts import (
    ComponentHealth,
    ComponentId,
    ContractModel,
    HealthStatus,
    JobError,
    JobProgress,
    JobStatus,
    JobSummary,
    SafeScalar,
)
from .service import Clock, utc_now

_CONFIG_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"
_JOB_ID_PATTERN = r"^bt_[0-9a-f]{20}$"
_MAX_METRICS = 256


class BacktestRunRequest(ContractModel):
    config_id: str = Field(min_length=1, max_length=128, pattern=_CONFIG_ID_PATTERN)


class BacktestResultReference(ContractModel):
    run_id: str = Field(min_length=1, max_length=256)
    bars_processed: int = Field(ge=0)
    restart_verified: bool = False


class BacktestJobRecord(ContractModel):
    job: JobSummary
    config_id: str = Field(min_length=1, max_length=128, pattern=_CONFIG_ID_PATTERN)
    result: BacktestResultReference | None = None


class BacktestCreateResponse(ContractModel):
    record: BacktestJobRecord
    created: bool


class BacktestResultSummary(ContractModel):
    job_id: str = Field(min_length=1, max_length=128)
    run_id: str = Field(min_length=1, max_length=256)
    bars_processed: int = Field(ge=0)
    restart_verified: bool = False
    metrics: dict[str, SafeScalar] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def validate_metrics(cls, value: dict[str, SafeScalar]) -> dict[str, SafeScalar]:
        return _safe_metrics(value)


class BacktestMetricChange(ContractModel):
    left: SafeScalar
    right: SafeScalar
    delta: float | None = None

    @field_validator("delta")
    @classmethod
    def validate_delta(cls, value: float | None) -> float | None:
        if value is not None and not math.isfinite(value):
            raise ValueError("delta must be finite")
        return value


class BacktestComparisonRequest(ContractModel):
    left_job_id: str = Field(min_length=1, max_length=128, pattern=_JOB_ID_PATTERN)
    right_job_id: str = Field(min_length=1, max_length=128, pattern=_JOB_ID_PATTERN)

    @model_validator(mode="after")
    def require_distinct_jobs(self) -> BacktestComparisonRequest:
        if self.left_job_id == self.right_job_id:
            raise ValueError("comparison requires two distinct backtest jobs")
        return self


class BacktestComparison(ContractModel):
    left_job_id: str = Field(min_length=1, max_length=128)
    right_job_id: str = Field(min_length=1, max_length=128)
    left_run_id: str = Field(min_length=1, max_length=256)
    right_run_id: str = Field(min_length=1, max_length=256)
    changes: dict[str, BacktestMetricChange] = Field(default_factory=dict)

    @field_validator("changes")
    @classmethod
    def validate_changes(
        cls,
        value: dict[str, BacktestMetricChange],
    ) -> dict[str, BacktestMetricChange]:
        if len(value) > _MAX_METRICS:
            raise ValueError(f"changes must contain at most {_MAX_METRICS} entries")
        normalized: dict[str, BacktestMetricChange] = {}
        for key, change in value.items():
            name = key.strip()
            if not name or len(name) > 128:
                raise ValueError("metric names must contain 1..128 non-whitespace characters")
            normalized[name] = change
        return dict(sorted(normalized.items()))


@dataclass(frozen=True, slots=True)
class BacktestExecution:
    run_id: str
    output_directory: Path
    bars_processed: int
    restart_verified: bool = False


@dataclass(frozen=True, slots=True)
class BacktestInspection:
    run_id: str
    metrics: Mapping[str, SafeScalar]


@dataclass(frozen=True, slots=True)
class BacktestComparisonData:
    left_run_id: str
    right_run_id: str
    changes: Mapping[str, BacktestMetricChange]


class BacktestRunner(Protocol):
    def run(self, config_id: str) -> BacktestExecution: ...

    def inspect(self, result_directory: Path) -> BacktestInspection: ...

    def compare(self, left: Path, right: Path) -> BacktestComparisonData: ...


class BacktestRunnerError(RuntimeError):
    def __init__(
        self,
        *,
        code: str,
        message: str,
        action: str | None,
        retryable: bool = False,
        details: Mapping[str, SafeScalar] | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.safe_message = message
        self.action = action
        self.retryable = retryable
        self.details = dict(details or {})


class BacktestControlError(RuntimeError):
    pass


class BacktestNotFound(BacktestControlError):
    pass


class BacktestIdempotencyConflict(BacktestControlError):
    pass


class BacktestResultUnavailable(BacktestControlError):
    pass


class SQLiteBacktestJobStore:
    def __init__(self, path: str | Path, *, clock: Clock = utc_now) -> None:
        self.path = Path(path).expanduser()
        if self.path.is_symlink():
            raise ValueError("backtest job store must not be a symlink")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._clock = clock
        self._initialize()
        self._fail_interrupted_jobs()

    def create_job(
        self,
        request: BacktestRunRequest,
        *,
        idempotency_key: str,
        actor_id: str,
    ) -> tuple[BacktestJobRecord, bool]:
        key = _bounded_identity(idempotency_key, "idempotency_key")
        actor = _bounded_identity(actor_id, "actor_id")
        request_hash = _request_hash(request)
        now = _iso(self._now())
        job_id = f"bt_{uuid4().hex[:20]}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM backtest_jobs WHERE actor_id = ? AND idempotency_key = ?",
                (actor, key),
            ).fetchone()
            if existing is not None:
                if str(existing["request_hash"]) != request_hash:
                    raise BacktestIdempotencyConflict(
                        "idempotency key is already bound to a different backtest request"
                    )
                connection.commit()
                return self._row_to_record(existing), False
            connection.execute(
                """
                INSERT INTO backtest_jobs (
                    job_id, actor_id, idempotency_key, request_hash, config_id,
                    status, phase, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    actor,
                    key,
                    request_hash,
                    request.config_id,
                    JobStatus.QUEUED.value,
                    "QUEUED",
                    now,
                    now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM backtest_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("created backtest job disappeared from durable store")
        return self._row_to_record(row), True

    def get_job(self, job_id: str) -> BacktestJobRecord:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM backtest_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise BacktestNotFound("backtest job was not found")
        return self._row_to_record(row)

    def list_jobs(self, *, limit: int = 50, offset: int = 0) -> tuple[BacktestJobRecord, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM backtest_jobs
                ORDER BY created_at DESC, job_id DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
        return tuple(self._row_to_record(row) for row in rows)

    def start_job(self, job_id: str) -> BacktestJobRecord:
        now = _iso(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM backtest_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise BacktestNotFound("backtest job was not found")
            if str(row["status"]) == JobStatus.QUEUED.value:
                connection.execute(
                    """
                    UPDATE backtest_jobs
                    SET status = ?, phase = ?, started_at = ?, updated_at = ?
                    WHERE job_id = ? AND status = ?
                    """,
                    (
                        JobStatus.RUNNING.value,
                        "EXECUTING",
                        now,
                        now,
                        job_id,
                        JobStatus.QUEUED.value,
                    ),
                )
                row = connection.execute(
                    "SELECT * FROM backtest_jobs WHERE job_id = ?",
                    (job_id,),
                ).fetchone()
            connection.commit()
        if row is None:
            raise RuntimeError("backtest job disappeared while starting")
        return self._row_to_record(row)

    def complete_job(self, job_id: str, execution: BacktestExecution) -> BacktestJobRecord:
        if not execution.run_id.strip():
            raise ValueError("run_id must be non-empty")
        if execution.bars_processed < 0:
            raise ValueError("bars_processed must be non-negative")
        result_path = execution.output_directory.expanduser().resolve()
        now = _iso(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE backtest_jobs
                SET status = ?, phase = ?, updated_at = ?, completed_at = ?,
                    run_id = ?, result_path = ?, bars_processed = ?, restart_verified = ?,
                    error_json = NULL
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.SUCCEEDED.value,
                    "COMPLETED",
                    now,
                    now,
                    execution.run_id,
                    str(result_path),
                    execution.bars_processed,
                    int(execution.restart_verified),
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM backtest_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise BacktestNotFound("backtest job was not found")
        if updated != 1:
            raise RuntimeError("backtest job cannot complete from its current state")
        return self._row_to_record(row)

    def fail_job(self, job_id: str, error: JobError) -> BacktestJobRecord:
        now = _iso(self._now())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            updated = connection.execute(
                """
                UPDATE backtest_jobs
                SET status = ?, phase = ?, updated_at = ?, completed_at = ?, error_json = ?
                WHERE job_id = ? AND status = ?
                """,
                (
                    JobStatus.FAILED.value,
                    "FAILED",
                    now,
                    now,
                    error.model_dump_json(),
                    job_id,
                    JobStatus.RUNNING.value,
                ),
            ).rowcount
            row = connection.execute(
                "SELECT * FROM backtest_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise BacktestNotFound("backtest job was not found")
        if updated != 1:
            raise RuntimeError("backtest job cannot fail from its current state")
        return self._row_to_record(row)

    def result_path(self, job_id: str) -> Path:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status, result_path FROM backtest_jobs WHERE job_id = ?",
                (job_id,),
            ).fetchone()
        if row is None:
            raise BacktestNotFound("backtest job was not found")
        if str(row["status"]) != JobStatus.SUCCEEDED.value or row["result_path"] is None:
            raise BacktestResultUnavailable("backtest result is not available for this job")
        return Path(str(row["result_path"]))

    def counts(self) -> dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM backtest_jobs GROUP BY status"
            ).fetchall()
        counts = {str(row["status"]): int(row["count"]) for row in rows}
        counts["TOTAL"] = sum(counts.values())
        return counts

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS backtest_jobs (
                    job_id TEXT PRIMARY KEY,
                    actor_id TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    request_hash TEXT NOT NULL,
                    config_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    phase TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    run_id TEXT,
                    result_path TEXT,
                    bars_processed INTEGER,
                    restart_verified INTEGER NOT NULL DEFAULT 0,
                    error_json TEXT,
                    UNIQUE(actor_id, idempotency_key)
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_backtest_jobs_created ON backtest_jobs(created_at)"
            )
            connection.execute("PRAGMA user_version = 1")
            connection.commit()

    def _fail_interrupted_jobs(self) -> None:
        now = _iso(self._now())
        error = JobError(
            code="BACKTEST_SERVER_INTERRUPTED",
            message="The Backtest worker stopped before the durable job completed.",
            action="Submit a new run with a new idempotency key after verifying local capacity.",
            retryable=True,
            error_id=f"bt_err_{uuid4().hex[:20]}",
            details={"reason": "worker_interrupted"},
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE backtest_jobs
                SET status = ?, phase = ?, updated_at = ?, completed_at = ?, error_json = ?
                WHERE status IN (?, ?, ?)
                """,
                (
                    JobStatus.FAILED.value,
                    "INTERRUPTED",
                    now,
                    now,
                    error.model_dump_json(),
                    JobStatus.STARTING.value,
                    JobStatus.RUNNING.value,
                    JobStatus.RECOVERING.value,
                ),
            )
            connection.commit()

    def _row_to_record(self, row: sqlite3.Row) -> BacktestJobRecord:
        status = JobStatus(str(row["status"]))
        error = JobError.model_validate_json(str(row["error_json"])) if row["error_json"] else None
        progress = None
        if status is JobStatus.RUNNING:
            progress = JobProgress(
                phase=str(row["phase"]),
                determinate=False,
                message="Backtest engine is executing; measured bar progress is not exposed yet.",
            )
        result = None
        if row["run_id"] is not None and row["bars_processed"] is not None:
            result = BacktestResultReference(
                run_id=str(row["run_id"]),
                bars_processed=int(row["bars_processed"]),
                restart_verified=bool(row["restart_verified"]),
            )
        job = JobSummary(
            job_id=str(row["job_id"]),
            component=ComponentId.BACKTEST,
            operation="run",
            status=status,
            phase=str(row["phase"]),
            progress=progress,
            created_at=_datetime(str(row["created_at"])),
            updated_at=_datetime(str(row["updated_at"])),
            started_at=_optional_datetime(row["started_at"]),
            completed_at=_optional_datetime(row["completed_at"]),
            error=error,
        )
        return BacktestJobRecord(job=job, config_id=str(row["config_id"]), result=result)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


class BacktestControlService:
    component = ComponentId.BACKTEST

    def __init__(
        self,
        store: SQLiteBacktestJobStore,
        runner: BacktestRunner,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._runner = runner
        self._clock = clock

    def health(self) -> ComponentHealth:
        try:
            counts = self._store.counts()
        except Exception as exc:
            return ComponentHealth(
                component=self.component,
                status=HealthStatus.UNAVAILABLE,
                checked_at=self._now(),
                summary="Backtest durable job store is unavailable",
                details={"error_type": type(exc).__name__, "reason": "job_store_unavailable"},
            )
        return ComponentHealth(
            component=self.component,
            status=HealthStatus.HEALTHY,
            checked_at=self._now(),
            summary="Backtest durable control is ready",
            details={
                "jobs_total": counts.get("TOTAL", 0),
                "jobs_running": counts.get(JobStatus.RUNNING.value, 0),
                "jobs_succeeded": counts.get(JobStatus.SUCCEEDED.value, 0),
                "jobs_failed": counts.get(JobStatus.FAILED.value, 0),
            },
        )

    def create_run(
        self,
        request: BacktestRunRequest,
        *,
        idempotency_key: str,
        actor_id: str,
    ) -> BacktestCreateResponse:
        record, created = self._store.create_job(
            request,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
        )
        return BacktestCreateResponse(record=record, created=created)

    def run_job(self, job_id: str) -> BacktestJobRecord:
        record = self._store.start_job(job_id)
        if record.job.status is not JobStatus.RUNNING:
            return record
        try:
            execution = self._runner.run(record.config_id)
        except BacktestRunnerError as exc:
            return self._store.fail_job(
                job_id,
                JobError(
                    code=exc.code,
                    message=exc.safe_message,
                    action=exc.action,
                    retryable=exc.retryable,
                    error_id=f"bt_err_{uuid4().hex[:20]}",
                    details=dict(exc.details),
                ),
            )
        except Exception as exc:
            return self._store.fail_job(
                job_id,
                JobError(
                    code="BACKTEST_EXECUTION_FAILED",
                    message="The Backtest engine could not complete the durable run.",
                    action="Open Logs & Evidence and inspect the local Backtest failure context.",
                    retryable=False,
                    error_id=f"bt_err_{uuid4().hex[:20]}",
                    details={"error_type": type(exc).__name__},
                ),
            )
        return self._store.complete_job(job_id, execution)

    def get_job(self, job_id: str) -> BacktestJobRecord:
        return self._store.get_job(job_id)

    def list_jobs(self, *, limit: int = 50, offset: int = 0) -> tuple[BacktestJobRecord, ...]:
        return self._store.list_jobs(limit=limit, offset=offset)

    def get_result(self, job_id: str) -> BacktestResultSummary:
        record = self._store.get_job(job_id)
        path = self._store.result_path(job_id)
        inspection = self._runner.inspect(path)
        if record.result is None or inspection.run_id != record.result.run_id:
            raise BacktestResultUnavailable(
                "backtest result identity no longer matches durable state"
            )
        return BacktestResultSummary(
            job_id=job_id,
            run_id=inspection.run_id,
            bars_processed=record.result.bars_processed,
            restart_verified=record.result.restart_verified,
            metrics=dict(inspection.metrics),
        )

    def compare(self, request: BacktestComparisonRequest) -> BacktestComparison:
        left_record = self._store.get_job(request.left_job_id)
        right_record = self._store.get_job(request.right_job_id)
        if left_record.result is None or right_record.result is None:
            raise BacktestResultUnavailable(
                "both backtest jobs must have durable result identities"
            )
        left = self._store.result_path(request.left_job_id)
        right = self._store.result_path(request.right_job_id)
        comparison = self._runner.compare(left, right)
        if (
            comparison.left_run_id != left_record.result.run_id
            or comparison.right_run_id != right_record.result.run_id
        ):
            raise BacktestResultUnavailable(
                "backtest comparison identities no longer match durable state"
            )
        return BacktestComparison(
            left_job_id=request.left_job_id,
            right_job_id=request.right_job_id,
            left_run_id=comparison.left_run_id,
            right_run_id=comparison.right_run_id,
            changes=dict(comparison.changes),
        )

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value.astimezone(UTC)


def _safe_metrics(value: Mapping[str, object]) -> dict[str, SafeScalar]:
    if len(value) > _MAX_METRICS:
        raise ValueError(f"metrics must contain at most {_MAX_METRICS} entries")
    normalized: dict[str, SafeScalar] = {}
    for key, item in value.items():
        name = key.strip()
        if not name or len(name) > 128:
            raise ValueError("metric names must contain 1..128 non-whitespace characters")
        if item is None or isinstance(item, str | bool | int):
            scalar: SafeScalar = item
        elif isinstance(item, float) and math.isfinite(item):
            scalar = item
        else:
            raise ValueError(f"metric {name} must be a finite scalar value")
        if isinstance(scalar, str) and len(scalar) > 1000:
            raise ValueError(f"metric {name} string value must contain at most 1000 characters")
        normalized[name] = scalar
    return dict(sorted(normalized.items()))


def _request_hash(request: BacktestRunRequest) -> str:
    payload = json.dumps(
        request.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode()
    return hashlib.sha256(payload).hexdigest()


def _bounded_identity(value: str, name: str) -> str:
    normalized = value.strip()
    if (
        not normalized
        or len(normalized) > 128
        or any(character in normalized for character in "\r\n")
    ):
        raise ValueError(f"{name} must contain 1..128 printable single-line characters")
    return normalized


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("durable backtest timestamp must be timezone-aware")
    return parsed.astimezone(UTC)


def _optional_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _datetime(str(value))
