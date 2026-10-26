from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from market_data_ingestion.enums import RunState

from .contracts import (
    JobCreatedResponse,
    JobDetails,
    JobError,
    JobEvent,
    JobListResponse,
    JobOperation,
    JobProgress,
)
from .errors import JobLeaseLostError, JobNotFoundError
from .job_store import SQLiteJobStore
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


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    owner_id: str
    lease_token: str
    generation: int
    acquired_at: datetime
    heartbeat_at: datetime
    expires_at: datetime


_ACTIVE_JOB_LEASE: ContextVar[JobLease | None] = ContextVar(
    "market_data_active_job_lease",
    default=None,
)


@contextmanager
def activate_job_lease(lease: JobLease) -> Iterator[None]:
    token = _ACTIVE_JOB_LEASE.set(lease)
    try:
        yield
    finally:
        _ACTIVE_JOB_LEASE.reset(token)


class SQLiteJobLeaseStore:
    """Atomic cross-process execution ownership stored beside operational jobs."""

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
                CREATE TABLE IF NOT EXISTS job_leases (
                    job_id TEXT PRIMARY KEY,
                    owner_id TEXT NOT NULL,
                    lease_token TEXT NOT NULL UNIQUE,
                    generation INTEGER NOT NULL CHECK(generation > 0),
                    acquired_at TEXT NOT NULL,
                    heartbeat_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    schema_version INTEGER NOT NULL CHECK(schema_version = 1),
                    FOREIGN KEY(job_id) REFERENCES jobs(job_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_job_leases_expiry
                    ON job_leases(expires_at, job_id);
                """
            )

    def claim(
        self,
        job_id: str,
        *,
        owner_id: str,
        ttl_seconds: float,
        at: datetime | None = None,
    ) -> JobLease | None:
        owner = self._identifier(owner_id, "owner_id", 128)
        ttl = self._ttl(ttl_seconds)
        now = self._timestamp(at)
        expires = now + ttl
        with self._connection(write=True) as connection:
            job = connection.execute("SELECT state FROM jobs WHERE job_id=?", (job_id,)).fetchone()
            if job is None:
                raise JobNotFoundError(job_id)
            if RunState(str(job["state"])) in _TERMINAL:
                connection.execute("DELETE FROM job_leases WHERE job_id=?", (job_id,))
                return None
            existing = connection.execute(
                "SELECT * FROM job_leases WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if existing is not None and self._datetime(str(existing["expires_at"])) > now:
                return None
            generation = 1 if existing is None else int(existing["generation"]) + 1
            lease_token = f"mdi_lease_{uuid4().hex}"
            values = (
                owner,
                lease_token,
                generation,
                now.isoformat(),
                now.isoformat(),
                expires.isoformat(),
                job_id,
            )
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO job_leases(
                        owner_id, lease_token, generation, acquired_at,
                        heartbeat_at, expires_at, job_id, schema_version
                    ) VALUES(?,?,?,?,?,?,?,1)
                    """,
                    values,
                )
            else:
                connection.execute(
                    """
                    UPDATE job_leases
                    SET owner_id=?, lease_token=?, generation=?, acquired_at=?,
                        heartbeat_at=?, expires_at=?, schema_version=1
                    WHERE job_id=?
                    """,
                    values,
                )
        return JobLease(
            job_id=job_id,
            owner_id=owner,
            lease_token=lease_token,
            generation=generation,
            acquired_at=now,
            heartbeat_at=now,
            expires_at=expires,
        )

    def renew(
        self,
        lease: JobLease,
        *,
        ttl_seconds: float,
        at: datetime | None = None,
    ) -> JobLease | None:
        ttl = self._ttl(ttl_seconds)
        now = self._timestamp(at)
        expires = now + ttl
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                UPDATE job_leases
                SET heartbeat_at=?, expires_at=?
                WHERE job_id=? AND owner_id=? AND lease_token=? AND generation=?
                    AND expires_at>?
                """,
                (
                    now.isoformat(),
                    expires.isoformat(),
                    lease.job_id,
                    lease.owner_id,
                    lease.lease_token,
                    lease.generation,
                    now.isoformat(),
                ),
            )
            if cursor.rowcount != 1:
                return None
        return JobLease(
            job_id=lease.job_id,
            owner_id=lease.owner_id,
            lease_token=lease.lease_token,
            generation=lease.generation,
            acquired_at=lease.acquired_at,
            heartbeat_at=now,
            expires_at=expires,
        )

    def release(self, lease: JobLease) -> bool:
        with self._connection(write=True) as connection:
            cursor = connection.execute(
                """
                DELETE FROM job_leases
                WHERE job_id=? AND owner_id=? AND lease_token=? AND generation=?
                """,
                (lease.job_id, lease.owner_id, lease.lease_token, lease.generation),
            )
            return cursor.rowcount == 1

    def is_current(self, lease: JobLease, *, at: datetime | None = None) -> bool:
        now = self._timestamp(at)
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM job_leases
                WHERE job_id=? AND owner_id=? AND lease_token=? AND generation=?
                    AND expires_at>?
                """,
                (
                    lease.job_id,
                    lease.owner_id,
                    lease.lease_token,
                    lease.generation,
                    now.isoformat(),
                ),
            ).fetchone()
        return row is not None

    def get(self, job_id: str) -> JobLease | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT * FROM job_leases WHERE job_id=?",
                (job_id,),
            ).fetchone()
        return None if row is None else self._lease(row)

    def reap_expired(self, *, at: datetime | None = None, limit: int = 500) -> tuple[str, ...]:
        if not 1 <= limit <= 5000:
            raise ValueError("invalid reap limit")
        now = self._timestamp(at)
        with self._connection(write=True) as connection:
            rows = connection.execute(
                """
                SELECT job_id FROM job_leases
                WHERE expires_at<=?
                ORDER BY expires_at ASC, job_id ASC LIMIT ?
                """,
                (now.isoformat(), limit),
            ).fetchall()
            job_ids = tuple(str(row["job_id"]) for row in rows)
            if job_ids:
                placeholders = ",".join("?" for _ in job_ids)
                connection.execute(
                    f"DELETE FROM job_leases WHERE job_id IN ({placeholders})",
                    job_ids,
                )
        return job_ids

    @classmethod
    def _lease(cls, row: sqlite3.Row) -> JobLease:
        return JobLease(
            job_id=str(row["job_id"]),
            owner_id=str(row["owner_id"]),
            lease_token=str(row["lease_token"]),
            generation=int(row["generation"]),
            acquired_at=cls._datetime(str(row["acquired_at"])),
            heartbeat_at=cls._datetime(str(row["heartbeat_at"])),
            expires_at=cls._datetime(str(row["expires_at"])),
        )

    @staticmethod
    def _ttl(value: float) -> timedelta:
        if not 1.0 <= value <= 3600.0:
            raise ValueError("lease ttl must be between 1 and 3600 seconds")
        return timedelta(seconds=value)

    @classmethod
    def _timestamp(cls, value: datetime | None) -> datetime:
        return datetime.now(UTC) if value is None else cls._aware(value)

    @staticmethod
    def _aware(value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("lease timestamps must be timezone-aware")
        return value.astimezone(UTC)

    @classmethod
    def _datetime(cls, value: str) -> datetime:
        return cls._aware(datetime.fromisoformat(value))

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


class FencedJobStore:
    """Job-store facade that rejects writes from a stale execution lease."""

    def __init__(self, store: SQLiteJobStore, leases: SQLiteJobLeaseStore) -> None:
        self._store = store
        self._leases = leases
        self.path = store.path

    def create_job(
        self,
        *,
        operation: JobOperation,
        request: BaseModel | Mapping[str, Any],
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str | None = None,
    ) -> JobCreatedResponse:
        return self._store.create_job(
            operation=operation,
            request=request,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )

    def create_repair_job(
        self,
        *,
        request: RepairJobRequest,
        confirmation_token: str,
        idempotency_key: str,
        correlation_id: str | None = None,
        actor_id: str,
    ) -> JobCreatedResponse:
        return self._store.create_repair_job(
            request=request,
            confirmation_token=confirmation_token,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )

    def get_job(self, job_id: str) -> JobDetails:
        return self._store.get_job(job_id)

    def list_jobs(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
        states: Sequence[RunState] = (),
        operations: Sequence[JobOperation] = (),
    ) -> JobListResponse:
        return self._store.list_jobs(
            limit=limit,
            offset=offset,
            states=states,
            operations=operations,
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
        self._assert_current(job_id)
        return self._store.transition(
            job_id,
            target,
            progress=progress,
            backend_run_id=backend_run_id,
            result=result,
            error=error,
            warnings=warnings,
        )

    def record_progress(self, job_id: str, progress: JobProgress) -> JobDetails:
        self._assert_current(job_id)
        return self._store.record_progress(job_id, progress)

    def events_after(
        self,
        job_id: str,
        *,
        after_event_id: int = 0,
        limit: int = 500,
    ) -> tuple[JobEvent, ...]:
        return self._store.events_after(job_id, after_event_id=after_event_id, limit=limit)

    def recoverable_jobs(self, *, limit: int = 200) -> tuple[JobDetails, ...]:
        return self._store.recoverable_jobs(limit=limit)

    def _assert_current(self, job_id: str) -> None:
        lease = _ACTIVE_JOB_LEASE.get()
        if lease is None:
            return
        if lease.job_id != job_id or not self._leases.is_current(lease):
            raise JobLeaseLostError(
                job_id=job_id,
                owner_id=lease.owner_id,
                generation=lease.generation,
            )
