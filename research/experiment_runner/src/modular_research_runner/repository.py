from __future__ import annotations

import hmac
import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from . import _repository_impl as _impl
from .canonical import CanonicalizationError, canonical_json_loads
from .engine import BacktestJob, BacktestRunReference
from .models import SpecificationError
from .state import (
    FailureRecord,
    RunState,
    RunStatus,
    StateTransitionError,
    ensure_transition,
)

StateRepositoryError = _impl.StateRepositoryError
_datetime = _impl._datetime
_failure_payload = _impl._failure_payload
_iso = _impl._iso
_job_payload = _impl._job_payload
_json = _impl._json
_require_row = _impl._require_row
_result_payload = _impl._result_payload

_raw_job = _impl._job
_raw_failure = _impl._failure
_raw_result = _impl._result


@dataclass(frozen=True, slots=True)
class RunLease:
    """Opaque, revision-bound authorization for one durable run claim."""

    run_id: str
    worker_id: str
    session_id: str
    lease_token: str
    revision: int

    def __post_init__(self) -> None:
        for name, value in (
            ("run_id", self.run_id),
            ("worker_id", self.worker_id),
            ("session_id", self.session_id),
            ("lease_token", self.lease_token),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SpecificationError(f"{name} must be non-empty")
        if (
            not isinstance(self.revision, int)
            or isinstance(self.revision, bool)
            or self.revision < 1
        ):
            raise SpecificationError("lease revision must be a positive integer")


def _payload(value: str, message: str) -> dict[str, Any]:
    try:
        raw = canonical_json_loads(value)
    except CanonicalizationError as exc:
        raise StateRepositoryError(message) from exc
    if not isinstance(raw, dict):
        raise StateRepositoryError(message)
    return raw


def _require_keys(raw: Mapping[str, Any], required: set[str], message: str) -> None:
    if required - set(raw):
        raise StateRepositoryError(message)


def _require_string(raw: Mapping[str, Any], key: str, message: str) -> None:
    if not isinstance(raw.get(key), str):
        raise StateRepositoryError(message)


def _require_integer(raw: Mapping[str, Any], key: str, message: str) -> None:
    value = raw.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise StateRepositoryError(message)


def _job(value: str) -> BacktestJob:
    message = "stored job payload is invalid"
    raw = _payload(value, message)
    _require_keys(
        raw,
        {
            "run_id",
            "experiment_id",
            "stage_id",
            "split_id",
            "dataset_id",
            "scenario_id",
            "seed",
            "parameters",
            "output_directory",
        },
        message,
    )
    for key in (
        "run_id",
        "experiment_id",
        "stage_id",
        "split_id",
        "dataset_id",
        "scenario_id",
    ):
        _require_string(raw, key, message)
    _require_integer(raw, "seed", message)
    if not isinstance(raw["parameters"], Mapping):
        raise StateRepositoryError(message)
    if not isinstance(raw["output_directory"], Path):
        raise StateRepositoryError(message)
    stage_role = raw.get("stage_role", "unspecified")
    if not isinstance(stage_role, str):
        raise StateRepositoryError(message)
    repetition_index = raw.get("repetition_index", 0)
    if not isinstance(repetition_index, int) or isinstance(repetition_index, bool):
        raise StateRepositoryError(message)
    metadata = raw.get("metadata", {})
    if not isinstance(metadata, Mapping):
        raise StateRepositoryError(message)
    period = raw.get("period")
    if period is not None and not isinstance(period, Mapping):
        raise StateRepositoryError(message)
    return _raw_job(value)


def _failure(value: str | None) -> FailureRecord | None:
    if value is None:
        return None
    message = "stored failure payload is invalid"
    raw = _payload(value, message)
    _require_keys(raw, {"category", "message", "occurred_at", "retryable"}, message)
    _require_string(raw, "category", message)
    _require_string(raw, "message", message)
    if not isinstance(raw["occurred_at"], datetime):
        raise StateRepositoryError(message)
    if not isinstance(raw["retryable"], bool):
        raise StateRepositoryError(message)
    exception_type = raw.get("exception_type")
    if exception_type is not None and not isinstance(exception_type, str):
        raise StateRepositoryError(message)
    return _raw_failure(value)


def _result(value: str | None) -> BacktestRunReference | None:
    if value is None:
        return None
    message = "stored result payload is invalid"
    raw = _payload(value, message)
    _require_keys(
        raw,
        {
            "requested_run_id",
            "engine_run_id",
            "output_directory",
            "bars_processed",
            "restart_verified",
        },
        message,
    )
    _require_string(raw, "requested_run_id", message)
    _require_string(raw, "engine_run_id", message)
    if not isinstance(raw["output_directory"], Path):
        raise StateRepositoryError(message)
    _require_integer(raw, "bars_processed", message)
    if not isinstance(raw["restart_verified"], bool):
        raise StateRepositoryError(message)
    return _raw_result(value)


# The implementation module is split only to keep the public boundary reviewable.
# Its repository methods resolve these codec functions from module globals, so install
# the strict validators once before exposing the repository class.
_impl._job = _job
_impl._failure = _failure
_impl._result = _result


class SqliteExperimentRepository(_impl.SqliteExperimentRepository):
    """Public repository boundary with revision-bound durable claim fencing."""

    SCHEMA_VERSION = 2

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception as exc:
            if connection.in_transaction:
                connection.rollback()
            if (
                isinstance(exc, sqlite3.Error)
                and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
            ):
                raise StateRepositoryError("state repository storage capacity exhausted") from None
            raise
        finally:
            connection.close()

    @staticmethod
    def _install_claim_schema(connection: sqlite3.Connection) -> None:
        _impl._execute_script_transactionally(
            connection,
            """
            CREATE TABLE IF NOT EXISTS run_claims (
                run_id TEXT PRIMARY KEY REFERENCES runs(run_id) ON DELETE CASCADE,
                worker_id TEXT NOT NULL,
                session_id TEXT NOT NULL,
                lease_token TEXT NOT NULL UNIQUE,
                claim_revision INTEGER NOT NULL CHECK(claim_revision >= 1)
            );
            CREATE TRIGGER IF NOT EXISTS trg_run_claim_cleanup
            AFTER UPDATE OF status ON runs
            WHEN NEW.status <> 'RUNNING'
            BEGIN
                DELETE FROM run_claims WHERE run_id = NEW.run_id;
            END;
            """,
        )

    def _install_schema_extensions(self, connection: sqlite3.Connection) -> None:
        self._install_claim_schema(connection)

    def _initialize(self) -> None:
        try:
            super()._initialize()
        except ValueError as exc:
            raise StateRepositoryError("unsupported state repository schema version") from exc
        except StateRepositoryError:
            with self._reader() as connection:
                row = connection.execute(
                    "SELECT value FROM repository_meta WHERE key = 'schema_version'"
                ).fetchone()
            if row is None:
                raise
            try:
                stored_version = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise StateRepositoryError("unsupported state repository schema version") from exc
            if stored_version != 1:
                raise
            with self._transaction() as connection:
                active = connection.execute(
                    "SELECT run_id FROM runs WHERE status = ? LIMIT 1",
                    (RunStatus.RUNNING.value,),
                ).fetchone()
                if active is not None:
                    raise StateRepositoryError(
                        "cannot migrate state repository with active run leases"
                    ) from None
                self._install_claim_schema(connection)
                connection.execute(
                    "UPDATE repository_meta SET value = ? WHERE key = 'schema_version'",
                    (str(self.SCHEMA_VERSION),),
                )

    @staticmethod
    def _validate_claim_identity(*, worker_id: str, session_id: str, lease_token: str) -> None:
        for name, value in (
            ("worker_id", worker_id),
            ("session_id", session_id),
            ("lease_token", lease_token),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SpecificationError(f"{name} must be non-empty")

    @staticmethod
    def _validate_revision(expected_revision: int) -> None:
        if (
            not isinstance(expected_revision, int)
            or isinstance(expected_revision, bool)
            or expected_revision < 1
        ):
            raise SpecificationError("expected_revision must be a positive integer")

    def _require_active_claim(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        worker_id: str,
        session_id: str,
        lease_token: str,
        expected_revision: int,
        at: datetime,
    ) -> sqlite3.Row:
        self._validate_claim_identity(
            worker_id=worker_id,
            session_id=session_id,
            lease_token=lease_token,
        )
        self._validate_revision(expected_revision)
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateRepositoryError("run is not registered")
        if RunStatus(row["status"]) is not RunStatus.RUNNING:
            raise StateTransitionError("run claim is no longer active")
        if row["revision"] != expected_revision or row["lease_owner"] != worker_id:
            raise StateTransitionError("run claim is no longer active")
        expires_at = row["lease_expires_at"]
        if expires_at is None or _datetime(expires_at) <= at:
            raise StateTransitionError("run claim is no longer active")
        claim = connection.execute(
            "SELECT worker_id, session_id, lease_token, claim_revision "
            "FROM run_claims WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        if claim is None:
            raise StateTransitionError("run claim is no longer active")
        if (
            claim["worker_id"] != worker_id
            or claim["session_id"] != session_id
            or claim["claim_revision"] != expected_revision
            or not hmac.compare_digest(claim["lease_token"], lease_token)
        ):
            raise StateTransitionError("run claim is no longer active")
        return row

    def claim_next(  # type: ignore[override]
        self,
        *,
        experiment_id: str,
        worker_id: str,
        session_id: str,
        lease_token: str,
        at: datetime,
        lease_seconds: int,
    ) -> tuple[BacktestJob, RunState, RunLease] | None:
        self._validate_claim_identity(
            worker_id=worker_id,
            session_id=session_id,
            lease_token=lease_token,
        )
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds < 1
        ):
            raise SpecificationError("lease_seconds must be positive")
        timestamp = _iso(at)
        lease_expires = at + timedelta(seconds=lease_seconds)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM runs WHERE experiment_id = ? AND status = ? "
                "AND available_at <= ? ORDER BY run_id LIMIT 1",
                (experiment_id, RunStatus.QUEUED.value, timestamp),
            ).fetchone()
            if row is None:
                return None
            ensure_transition(RunStatus.QUEUED, RunStatus.RUNNING)
            revision = int(row["revision"]) + 1
            attempt = int(row["attempt_count"]) + 1
            connection.execute(
                "UPDATE runs SET status = ?, attempt_count = ?, lease_owner = ?, "
                "lease_expires_at = ?, failure_json = NULL, updated_at = ?, revision = ? "
                "WHERE run_id = ?",
                (
                    RunStatus.RUNNING.value,
                    attempt,
                    worker_id,
                    _iso(lease_expires),
                    timestamp,
                    revision,
                    row["run_id"],
                ),
            )
            connection.execute(
                "INSERT INTO run_claims(run_id, worker_id, session_id, lease_token, "
                "claim_revision) VALUES(?, ?, ?, ?, ?)",
                (row["run_id"], worker_id, session_id, lease_token, revision),
            )
            connection.execute(
                "INSERT INTO run_events(run_id, from_status, to_status, occurred_at, "
                "revision, detail) VALUES(?, ?, ?, ?, ?, ?)",
                (
                    row["run_id"],
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                    timestamp,
                    revision,
                    f"claimed:{worker_id}",
                ),
            )
            updated = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (row["run_id"],)
            ).fetchone()
            required = _require_row(updated, operation="run claim")
            lease = RunLease(
                run_id=required["run_id"],
                worker_id=worker_id,
                session_id=session_id,
                lease_token=lease_token,
                revision=revision,
            )
            return _job(required["job_json"]), self._state(required), lease

    def heartbeat(  # type: ignore[override]
        self,
        run_id: str,
        *,
        worker_id: str,
        session_id: str,
        lease_token: str,
        expected_revision: int,
        at: datetime,
        lease_seconds: int,
    ) -> tuple[RunState, RunLease]:
        if (
            not isinstance(lease_seconds, int)
            or isinstance(lease_seconds, bool)
            or lease_seconds < 1
        ):
            raise SpecificationError("lease_seconds must be positive")
        with self._transaction() as connection:
            self._require_active_claim(
                connection,
                run_id=run_id,
                worker_id=worker_id,
                session_id=session_id,
                lease_token=lease_token,
                expected_revision=expected_revision,
                at=at,
            )
            revision = expected_revision + 1
            connection.execute(
                "UPDATE runs SET lease_expires_at = ?, updated_at = ?, revision = ? "
                "WHERE run_id = ?",
                (_iso(at + timedelta(seconds=lease_seconds)), _iso(at), revision, run_id),
            )
            connection.execute(
                "UPDATE run_claims SET claim_revision = ? WHERE run_id = ?",
                (revision, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            state = self._state(_require_row(updated, operation="heartbeat"))
            return state, RunLease(
                run_id=run_id,
                worker_id=worker_id,
                session_id=session_id,
                lease_token=lease_token,
                revision=revision,
            )

    def succeed(  # type: ignore[override]
        self,
        run_id: str,
        *,
        worker_id: str,
        session_id: str,
        lease_token: str,
        expected_revision: int,
        result: BacktestRunReference,
        at: datetime,
    ) -> RunState:
        if result.requested_run_id != run_id:
            raise StateTransitionError("result does not belong to the claimed run")
        with self._transaction() as connection:
            self._require_active_claim(
                connection,
                run_id=run_id,
                worker_id=worker_id,
                session_id=session_id,
                lease_token=lease_token,
                expected_revision=expected_revision,
                at=at,
            )
            row = self._transition(
                connection,
                run_id=run_id,
                target=RunStatus.SUCCEEDED,
                at=at,
                expected_owner=worker_id,
                result=result,
                detail="completed",
            )
            return self._state(row)

    def fail(  # type: ignore[override]
        self,
        run_id: str,
        *,
        worker_id: str,
        session_id: str,
        lease_token: str,
        expected_revision: int,
        failure: FailureRecord,
        retry_at: datetime | None,
        at: datetime,
    ) -> RunState:
        with self._transaction() as connection:
            current = self._require_active_claim(
                connection,
                run_id=run_id,
                worker_id=worker_id,
                session_id=session_id,
                lease_token=lease_token,
                expected_revision=expected_revision,
                at=at,
            )
            retry = (
                retry_at is not None
                and failure.retryable
                and int(current["attempt_count"]) < int(current["max_attempts"])
            )
            target = RunStatus.RETRY_WAIT if retry else RunStatus.FAILED
            row = self._transition(
                connection,
                run_id=run_id,
                target=target,
                at=at,
                expected_owner=worker_id,
                available_at=retry_at,
                failure=failure,
                detail=f"failure:{failure.category.value}",
            )
            return self._state(row)

    def cancel_claim(
        self,
        run_id: str,
        *,
        worker_id: str,
        session_id: str,
        lease_token: str,
        expected_revision: int,
        at: datetime,
    ) -> RunState:
        with self._transaction() as connection:
            self._require_active_claim(
                connection,
                run_id=run_id,
                worker_id=worker_id,
                session_id=session_id,
                lease_token=lease_token,
                expected_revision=expected_revision,
                at=at,
            )
            row = self._transition(
                connection,
                run_id=run_id,
                target=RunStatus.CANCELED,
                at=at,
                expected_owner=worker_id,
                detail="canceled-active-claim",
            )
            return self._state(row)
