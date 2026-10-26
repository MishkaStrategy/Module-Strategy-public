from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .access import FinalTestAccessError, FinalTestGrant
from .canonical import canonical_json_bytes, canonical_json_loads
from .effective_config import EffectiveConfigArtifactReference
from .engine import BacktestJob, BacktestRunReference
from .models import ExperimentStatus, SpecificationError, TimeRange
from .state import (
    FailureCategory,
    FailureRecord,
    RunState,
    RunStatus,
    StateEvent,
    StateTransitionError,
    ensure_transition,
)


class StateRepositoryError(RuntimeError):
    """Raised when durable experiment state is missing, conflicting, or corrupted."""


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SpecificationError("repository timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _datetime(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError as exc:
        raise StateRepositoryError("stored timestamp is invalid") from exc


def _json(value: Any) -> str:
    return canonical_json_bytes(value).decode("utf-8")


def _job_payload(job: BacktestJob) -> Mapping[str, Any]:
    return {
        "run_id": job.run_id,
        "experiment_id": job.experiment_id,
        "stage_id": job.stage_id,
        "split_id": job.split_id,
        "dataset_id": job.dataset_id,
        "scenario_id": job.scenario_id,
        "seed": job.seed,
        "parameters": job.parameters,
        "output_directory": job.output_directory,
        "stage_role": job.stage_role,
        "period": job.period,
        "repetition_index": job.repetition_index,
        "metadata": job.metadata,
    }


def _job(value: str) -> BacktestJob:
    raw = canonical_json_loads(value)
    if not isinstance(raw, dict):
        raise StateRepositoryError("stored job payload is invalid")
    try:
        period_raw = raw.get("period")
        period = None
        if period_raw is not None:
            if not isinstance(period_raw, dict):
                raise TypeError("period is not a mapping")
            period = TimeRange(period_raw["start"], period_raw["end"])
        return BacktestJob(
            run_id=str(raw["run_id"]),
            experiment_id=str(raw["experiment_id"]),
            stage_id=str(raw["stage_id"]),
            split_id=str(raw["split_id"]),
            dataset_id=str(raw["dataset_id"]),
            scenario_id=str(raw["scenario_id"]),
            seed=int(raw["seed"]),
            parameters=raw["parameters"],
            output_directory=Path(raw["output_directory"]),
            stage_role=str(raw.get("stage_role", "unspecified")),
            period=period,
            repetition_index=int(raw.get("repetition_index", 0)),
            metadata=raw.get("metadata", {}),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateRepositoryError("stored job payload is invalid") from exc


def _failure_payload(value: FailureRecord) -> Mapping[str, Any]:
    return {
        "category": value.category.value,
        "message": value.message,
        "occurred_at": value.occurred_at,
        "retryable": value.retryable,
        "exception_type": value.exception_type,
    }


def _failure(value: str | None) -> FailureRecord | None:
    if value is None:
        return None
    raw = canonical_json_loads(value)
    if not isinstance(raw, dict):
        raise StateRepositoryError("stored failure payload is invalid")
    try:
        return FailureRecord(
            category=FailureCategory(raw["category"]),
            message=str(raw["message"]),
            occurred_at=raw["occurred_at"],
            retryable=bool(raw["retryable"]),
            exception_type=(
                str(raw["exception_type"]) if raw.get("exception_type") is not None else None
            ),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateRepositoryError("stored failure payload is invalid") from exc


def _result_payload(value: BacktestRunReference) -> Mapping[str, Any]:
    payload: dict[str, Any] = {
        "requested_run_id": value.requested_run_id,
        "engine_run_id": value.engine_run_id,
        "output_directory": value.output_directory,
        "bars_processed": value.bars_processed,
        "restart_verified": value.restart_verified,
    }
    if value.effective_config_artifact is not None:
        payload["effective_config_artifact"] = {
            "relative_path": value.effective_config_artifact.relative_path,
            "sha256": value.effective_config_artifact.sha256,
            "size_bytes": value.effective_config_artifact.size_bytes,
            "sidecar_root": value.effective_config_artifact.sidecar_root,
        }
    return payload


def _result(value: str | None) -> BacktestRunReference | None:
    if value is None:
        return None
    raw = canonical_json_loads(value)
    if not isinstance(raw, dict):
        raise StateRepositoryError("stored result payload is invalid")
    try:
        base_keys = {
            "requested_run_id",
            "engine_run_id",
            "output_directory",
            "bars_processed",
            "restart_verified",
        }
        if frozenset(raw) not in {
            frozenset(base_keys),
            frozenset(base_keys | {"effective_config_artifact"}),
        }:
            raise TypeError("stored result payload fields are invalid")
        artifact_raw = raw.get("effective_config_artifact")
        artifact = None
        if artifact_raw is not None:
            if not isinstance(artifact_raw, dict) or set(artifact_raw) not in (
                {"relative_path", "sha256", "size_bytes"},
                {"relative_path", "sha256", "size_bytes", "sidecar_root"},
            ):
                raise TypeError("stored effective config artifact reference is invalid")
            artifact = EffectiveConfigArtifactReference(
                relative_path=artifact_raw["relative_path"],
                sha256=artifact_raw["sha256"],
                size_bytes=artifact_raw["size_bytes"],
                sidecar_root=artifact_raw.get("sidecar_root"),
            )
        return BacktestRunReference(
            requested_run_id=str(raw["requested_run_id"]),
            engine_run_id=str(raw["engine_run_id"]),
            output_directory=Path(raw["output_directory"]),
            bars_processed=int(raw["bars_processed"]),
            restart_verified=bool(raw["restart_verified"]),
            effective_config_artifact=artifact,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise StateRepositoryError("stored result payload is invalid") from exc


def _require_row(row: sqlite3.Row | None, *, operation: str) -> sqlite3.Row:
    if row is None:
        raise StateRepositoryError(f"repository row disappeared after {operation}")
    return row


def _execute_script_transactionally(connection: sqlite3.Connection, script: str) -> None:
    """Execute a multi-statement schema script without implicit commits."""

    pending: list[str] = []
    for line in script.splitlines():
        if not pending and not line.strip():
            continue
        pending.append(line)
        statement = "\n".join(pending).strip()
        if statement and sqlite3.complete_statement(statement):
            connection.execute(statement)
            pending.clear()
    if any(line.strip() for line in pending):
        raise StateRepositoryError("schema script is incomplete")


class SqliteExperimentRepository:
    """Transactional, restart-safe experiment and run repository.

    Every mutating operation uses ``BEGIN IMMEDIATE``. Claims are therefore atomic
    across threads and processes that share the same SQLite database.
    """

    SCHEMA_VERSION = 1

    def __init__(self, path: Path) -> None:
        self.path = path
        if path.is_symlink():
            raise StateRepositoryError("state database must not be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and not path.is_file():
            raise StateRepositoryError("state database path must be a regular file")
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _install_schema_extensions(self, connection: sqlite3.Connection) -> None:
        """Install subclass-owned schema objects inside the active transaction."""

        return None

    def _initialize(self) -> None:
        with self._transaction() as connection:
            _execute_script_transactionally(
                connection,
                """
                CREATE TABLE IF NOT EXISTS repository_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS experiments (
                    experiment_id TEXT PRIMARY KEY,
                    specification_sha256 TEXT NOT NULL,
                    manifest_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    job_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt_count INTEGER NOT NULL,
                    max_attempts INTEGER NOT NULL,
                    available_at TEXT NOT NULL,
                    lease_owner TEXT,
                    lease_expires_at TEXT,
                    failure_json TEXT,
                    result_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    revision INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_runs_claim
                    ON runs(experiment_id, status, available_at, run_id);
                CREATE TABLE IF NOT EXISTS run_events (
                    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL REFERENCES runs(run_id),
                    from_status TEXT,
                    to_status TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    revision INTEGER NOT NULL,
                    detail TEXT
                );
                CREATE TABLE IF NOT EXISTS final_test_grants (
                    grant_id TEXT PRIMARY KEY,
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    specification_sha256 TEXT NOT NULL,
                    split_fingerprint TEXT NOT NULL,
                    candidates_json TEXT NOT NULL,
                    issued_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_final_test_grant_scope
                    ON final_test_grants(
                        experiment_id, specification_sha256, split_fingerprint, candidates_json
                    );
                CREATE TABLE IF NOT EXISTS stage_activations (
                    experiment_id TEXT NOT NULL REFERENCES experiments(experiment_id),
                    target_role TEXT NOT NULL,
                    candidates_json TEXT NOT NULL,
                    activated_at TEXT NOT NULL,
                    PRIMARY KEY(experiment_id, target_role)
                );
                """,
            )
            self._install_schema_extensions(connection)
            row = connection.execute(
                "SELECT value FROM repository_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO repository_meta(key, value) VALUES('schema_version', ?)",
                    (str(self.SCHEMA_VERSION),),
                )
            elif int(row["value"]) != self.SCHEMA_VERSION:
                raise StateRepositoryError("unsupported state repository schema version")

    def register_experiment(
        self,
        *,
        experiment_id: str,
        specification_sha256: str,
        manifest: Mapping[str, Any],
        created_at: datetime,
    ) -> None:
        timestamp = _iso(created_at)
        manifest_json = _json(manifest)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT specification_sha256, manifest_json FROM experiments "
                "WHERE experiment_id = ?",
                (experiment_id,),
            ).fetchone()
            if existing is not None:
                if (
                    existing["specification_sha256"] != specification_sha256
                    or existing["manifest_json"] != manifest_json
                ):
                    raise StateRepositoryError(
                        "experiment ID is already registered with different provenance"
                    )
                return
            connection.execute(
                "INSERT INTO experiments VALUES(?, ?, ?, ?, ?, ?)",
                (
                    experiment_id,
                    specification_sha256,
                    manifest_json,
                    ExperimentStatus.CREATED.value,
                    timestamp,
                    timestamp,
                ),
            )

    def experiment_status(self, experiment_id: str) -> ExperimentStatus:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT status FROM experiments WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
        if row is None:
            raise StateRepositoryError("experiment is not registered")
        try:
            return ExperimentStatus(row["status"])
        except ValueError as exc:
            raise StateRepositoryError("stored experiment status is invalid") from exc

    def set_experiment_status(
        self, experiment_id: str, status: ExperimentStatus, *, at: datetime
    ) -> None:
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE experiments SET status = ?, updated_at = ? WHERE experiment_id = ?",
                (status.value, _iso(at), experiment_id),
            ).rowcount
            if changed != 1:
                raise StateRepositoryError("experiment is not registered")

    def register_jobs(
        self,
        jobs: Iterable[BacktestJob],
        *,
        max_attempts: int,
        created_at: datetime,
    ) -> int:
        if max_attempts < 1:
            raise SpecificationError("max_attempts must be positive")
        timestamp = _iso(created_at)
        inserted = 0
        with self._transaction() as connection:
            for job in sorted(jobs, key=lambda item: item.run_id):
                payload = _json(_job_payload(job))
                existing = connection.execute(
                    "SELECT job_json, max_attempts FROM runs WHERE run_id = ?", (job.run_id,)
                ).fetchone()
                if existing is not None:
                    if existing["job_json"] != payload or existing["max_attempts"] != max_attempts:
                        raise StateRepositoryError(
                            f"run {job.run_id} is already registered with different inputs"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, experiment_id, job_json, status, attempt_count,
                        max_attempts, available_at, lease_owner, lease_expires_at,
                        failure_json, result_json, created_at, updated_at, revision
                    ) VALUES(?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, NULL, ?, ?, 0)
                    """,
                    (
                        job.run_id,
                        job.experiment_id,
                        payload,
                        RunStatus.PLANNED.value,
                        max_attempts,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO run_events(run_id, from_status, to_status, occurred_at, "
                    "revision, detail) VALUES(?, NULL, ?, ?, 0, ?)",
                    (job.run_id, RunStatus.PLANNED.value, timestamp, "registered"),
                )
                inserted += 1
        return inserted

    def _transition(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        target: RunStatus,
        at: datetime,
        expected_owner: str | None = None,
        available_at: datetime | None = None,
        failure: FailureRecord | None = None,
        result: BacktestRunReference | None = None,
        detail: str | None = None,
    ) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateRepositoryError("run is not registered")
        current = RunStatus(row["status"])
        ensure_transition(current, target)
        if expected_owner is not None and row["lease_owner"] != expected_owner:
            raise StateTransitionError("run lease is not owned by this worker")
        revision = int(row["revision"]) + 1
        timestamp = _iso(at)
        next_available = _iso(available_at if available_at is not None else at)
        failure_json = _json(_failure_payload(failure)) if failure is not None else None
        result_json = _json(_result_payload(result)) if result is not None else None
        connection.execute(
            """
            UPDATE runs SET status = ?, available_at = ?, lease_owner = NULL,
                lease_expires_at = NULL, failure_json = ?, result_json = ?,
                updated_at = ?, revision = ? WHERE run_id = ?
            """,
            (
                target.value,
                next_available,
                failure_json,
                result_json,
                timestamp,
                revision,
                run_id,
            ),
        )
        connection.execute(
            "INSERT INTO run_events(run_id, from_status, to_status, occurred_at, revision, "
            "detail) VALUES(?, ?, ?, ?, ?, ?)",
            (run_id, current.value, target.value, timestamp, revision, detail),
        )
        updated = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return _require_row(updated, operation="run transition")

    def queue_planned(self, experiment_id: str, *, at: datetime) -> int:
        changed = 0
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT run_id FROM runs WHERE experiment_id = ? "
                "AND ((status = ?) OR (status = ? AND available_at <= ?)) "
                "ORDER BY run_id",
                (
                    experiment_id,
                    RunStatus.PLANNED.value,
                    RunStatus.RETRY_WAIT.value,
                    _iso(at),
                ),
            ).fetchall()
            for row in rows:
                self._transition(
                    connection,
                    run_id=row["run_id"],
                    target=RunStatus.QUEUED,
                    at=at,
                    detail="queued",
                )
                changed += 1
        return changed

    def claim_next(
        self,
        *,
        experiment_id: str,
        worker_id: str,
        at: datetime,
        lease_seconds: int,
    ) -> tuple[BacktestJob, RunState] | None:
        if not worker_id.strip():
            raise SpecificationError("worker_id must be non-empty")
        if lease_seconds < 1:
            raise SpecificationError("lease_seconds must be positive")
        timestamp = _iso(at)
        lease_expires = at + timedelta(seconds=lease_seconds)
        with self._transaction() as connection:
            row = connection.execute(
                """
                SELECT * FROM runs WHERE experiment_id = ? AND status = ?
                    AND available_at <= ? ORDER BY run_id LIMIT 1
                """,
                (experiment_id, RunStatus.QUEUED.value, timestamp),
            ).fetchone()
            if row is None:
                return None
            ensure_transition(RunStatus.QUEUED, RunStatus.RUNNING)
            revision = int(row["revision"]) + 1
            attempt = int(row["attempt_count"]) + 1
            connection.execute(
                """
                UPDATE runs SET status = ?, attempt_count = ?, lease_owner = ?,
                    lease_expires_at = ?, failure_json = NULL, updated_at = ?, revision = ?
                    WHERE run_id = ?
                """,
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
            return _job(required["job_json"]), self._state(required)

    def heartbeat(
        self,
        run_id: str,
        *,
        worker_id: str,
        at: datetime,
        lease_seconds: int,
    ) -> RunState:
        with self._transaction() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if row is None:
                raise StateRepositoryError("run is not registered")
            if RunStatus(row["status"]) is not RunStatus.RUNNING:
                raise StateTransitionError("only a running job can heartbeat")
            if row["lease_owner"] != worker_id:
                raise StateTransitionError("run lease is not owned by this worker")
            revision = int(row["revision"]) + 1
            connection.execute(
                "UPDATE runs SET lease_expires_at = ?, updated_at = ?, revision = ? "
                "WHERE run_id = ?",
                (_iso(at + timedelta(seconds=lease_seconds)), _iso(at), revision, run_id),
            )
            updated = connection.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            return self._state(_require_row(updated, operation="heartbeat"))

    def succeed(
        self,
        run_id: str,
        *,
        worker_id: str,
        result: BacktestRunReference,
        at: datetime,
    ) -> RunState:
        if result.requested_run_id != run_id:
            raise StateTransitionError("result does not belong to the claimed run")
        with self._transaction() as connection:
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

    def fail(
        self,
        run_id: str,
        *,
        worker_id: str,
        failure: FailureRecord,
        retry_at: datetime | None,
        at: datetime,
    ) -> RunState:
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT attempt_count, max_attempts FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if current is None:
                raise StateRepositoryError("run is not registered")
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

    def cancel_pending(self, experiment_id: str, *, at: datetime) -> int:
        changed = 0
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT run_id, status FROM runs WHERE experiment_id = ? ORDER BY run_id",
                (experiment_id,),
            ).fetchall()
            for row in rows:
                status = RunStatus(row["status"])
                if status in {RunStatus.PLANNED, RunStatus.QUEUED, RunStatus.RETRY_WAIT}:
                    self._transition(
                        connection,
                        run_id=row["run_id"],
                        target=RunStatus.CANCELED,
                        at=at,
                        detail="experiment canceled",
                    )
                    changed += 1
        return changed

    def recover_expired_leases(self, *, experiment_id: str, at: datetime) -> int:
        timestamp = _iso(at)
        changed = 0
        with self._transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM runs WHERE experiment_id = ? AND status = ?
                    AND lease_expires_at < ? ORDER BY run_id
                """,
                (experiment_id, RunStatus.RUNNING.value, timestamp),
            ).fetchall()
            for row in rows:
                retry = int(row["attempt_count"]) < int(row["max_attempts"])
                failure = FailureRecord(
                    category=FailureCategory.TRANSIENT,
                    message="worker lease expired before completion",
                    occurred_at=at,
                    retryable=retry,
                    exception_type="LeaseExpired",
                )
                target = RunStatus.QUEUED if retry else RunStatus.FAILED
                self._transition(
                    connection,
                    run_id=row["run_id"],
                    target=(RunStatus.RETRY_WAIT if retry else RunStatus.FAILED),
                    at=at,
                    available_at=at,
                    failure=failure,
                    detail="expired lease recovered",
                )
                if target is RunStatus.QUEUED:
                    self._transition(
                        connection,
                        run_id=row["run_id"],
                        target=RunStatus.QUEUED,
                        at=at,
                        detail="requeued after lease expiry",
                    )
                changed += 1
        return changed

    def activate_stage(
        self,
        *,
        experiment_id: str,
        target_role: str,
        candidate_configuration_hashes: Iterable[str],
        jobs: Iterable[BacktestJob],
        max_attempts: int,
        activated_at: datetime,
    ) -> int:
        if not target_role.strip() or target_role in {"train", "final_untouched"}:
            raise StateRepositoryError("target role is not an activatable research stage")
        if max_attempts < 1:
            raise SpecificationError("max_attempts must be positive")
        candidates = tuple(sorted(set(candidate_configuration_hashes)))
        if not candidates:
            raise StateRepositoryError("stage activation requires frozen candidates")
        ordered_jobs = tuple(sorted(jobs, key=lambda item: item.run_id))
        if not ordered_jobs:
            raise StateRepositoryError("stage activation requires jobs")
        timestamp = _iso(activated_at)
        candidates_json = _json(candidates)
        with self._transaction() as connection:
            experiment = connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id = ?", (experiment_id,)
            ).fetchone()
            if experiment is None:
                raise StateRepositoryError("experiment is not registered")
            existing_activation = connection.execute(
                "SELECT candidates_json FROM stage_activations "
                "WHERE experiment_id = ? AND target_role = ?",
                (experiment_id, target_role),
            ).fetchone()
            if existing_activation is not None:
                if existing_activation["candidates_json"] != candidates_json:
                    raise StateRepositoryError(
                        "stage was already activated with a different frozen candidate set"
                    )
            else:
                connection.execute(
                    "INSERT INTO stage_activations VALUES(?, ?, ?, ?)",
                    (experiment_id, target_role, candidates_json, timestamp),
                )
            inserted = 0
            allowed = set(candidates)
            for job in ordered_jobs:
                if job.experiment_id != experiment_id or job.stage_role != target_role:
                    raise StateRepositoryError("stage activation job scope is invalid")
                candidate = job.metadata.get("configuration_sha256")
                if not isinstance(candidate, str) or candidate not in allowed:
                    raise StateRepositoryError("stage activation job is not a frozen candidate")
                payload = _json(_job_payload(job))
                existing = connection.execute(
                    "SELECT job_json, max_attempts FROM runs WHERE run_id = ?", (job.run_id,)
                ).fetchone()
                if existing is not None:
                    if existing["job_json"] != payload or existing["max_attempts"] != max_attempts:
                        raise StateRepositoryError(
                            f"run {job.run_id} is already registered with different inputs"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, experiment_id, job_json, status, attempt_count,
                        max_attempts, available_at, lease_owner, lease_expires_at,
                        failure_json, result_json, created_at, updated_at, revision
                    ) VALUES(?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, NULL, ?, ?, 0)
                    """,
                    (
                        job.run_id,
                        job.experiment_id,
                        payload,
                        RunStatus.PLANNED.value,
                        max_attempts,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO run_events(run_id, from_status, to_status, occurred_at, "
                    "revision, detail) VALUES(?, NULL, ?, ?, 0, ?)",
                    (
                        job.run_id,
                        RunStatus.PLANNED.value,
                        timestamp,
                        f"stage activated:{target_role}",
                    ),
                )
                inserted += 1
        return inserted

    def stage_activation_candidates(
        self, experiment_id: str, target_role: str
    ) -> tuple[str, ...] | None:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT candidates_json FROM stage_activations "
                "WHERE experiment_id = ? AND target_role = ?",
                (experiment_id, target_role),
            ).fetchone()
        if row is None:
            return None
        value = canonical_json_loads(row["candidates_json"])
        if not isinstance(value, tuple) or any(not isinstance(item, str) for item in value):
            raise StateRepositoryError("stored stage activation candidates are invalid")
        return value

    def issue_final_test_grant(self, grant: FinalTestGrant) -> FinalTestGrant:
        candidates_json = _json(grant.candidate_configuration_hashes)
        with self._transaction() as connection:
            experiment = connection.execute(
                "SELECT specification_sha256 FROM experiments WHERE experiment_id = ?",
                (grant.experiment_id,),
            ).fetchone()
            if experiment is None:
                raise StateRepositoryError("experiment is not registered")
            if experiment["specification_sha256"] != grant.specification_sha256:
                raise FinalTestAccessError("grant specification does not match the experiment")
            existing = connection.execute(
                "SELECT * FROM final_test_grants WHERE grant_id = ?", (grant.grant_id,)
            ).fetchone()
            if existing is not None:
                restored = self._grant(existing)
                if (
                    restored.experiment_id != grant.experiment_id
                    or restored.specification_sha256 != grant.specification_sha256
                    or restored.split_fingerprint != grant.split_fingerprint
                    or restored.candidate_configuration_hashes
                    != grant.candidate_configuration_hashes
                ):
                    raise FinalTestAccessError("grant ID is already bound to different inputs")
                return restored
            connection.execute(
                """
                INSERT INTO final_test_grants(
                    grant_id, experiment_id, specification_sha256, split_fingerprint,
                    candidates_json, issued_at, consumed_at
                ) VALUES(?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    grant.grant_id,
                    grant.experiment_id,
                    grant.specification_sha256,
                    grant.split_fingerprint,
                    candidates_json,
                    _iso(grant.issued_at),
                ),
            )
        return grant

    def activate_final_test_grant(
        self,
        grant_id: str,
        *,
        jobs: Iterable[BacktestJob],
        max_attempts: int,
        activated_at: datetime,
    ) -> tuple[FinalTestGrant, int]:
        if max_attempts < 1:
            raise SpecificationError("max_attempts must be positive")
        ordered_jobs = tuple(sorted(jobs, key=lambda item: item.run_id))
        if not ordered_jobs:
            raise FinalTestAccessError("final test activation requires jobs")
        timestamp = _iso(activated_at)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM final_test_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            if row is None:
                raise FinalTestAccessError("final test grant does not exist")
            if row["consumed_at"] is not None:
                raise FinalTestAccessError("final test grant has already been consumed")
            grant = self._grant(row)
            allowed = set(grant.candidate_configuration_hashes)
            inserted = 0
            for job in ordered_jobs:
                if job.experiment_id != grant.experiment_id:
                    raise FinalTestAccessError("final test job belongs to another experiment")
                if job.stage_role != "final_untouched":
                    raise FinalTestAccessError("grant may activate only final untouched jobs")
                candidate = job.metadata.get("configuration_sha256")
                if not isinstance(candidate, str) or candidate not in allowed:
                    raise FinalTestAccessError("final test job is not a frozen candidate")
                payload = _json(_job_payload(job))
                existing = connection.execute(
                    "SELECT job_json, max_attempts FROM runs WHERE run_id = ?", (job.run_id,)
                ).fetchone()
                if existing is not None:
                    if existing["job_json"] != payload or existing["max_attempts"] != max_attempts:
                        raise StateRepositoryError(
                            f"run {job.run_id} is already registered with different inputs"
                        )
                    continue
                connection.execute(
                    """
                    INSERT INTO runs(
                        run_id, experiment_id, job_json, status, attempt_count,
                        max_attempts, available_at, lease_owner, lease_expires_at,
                        failure_json, result_json, created_at, updated_at, revision
                    ) VALUES(?, ?, ?, ?, 0, ?, ?, NULL, NULL, NULL, NULL, ?, ?, 0)
                    """,
                    (
                        job.run_id,
                        job.experiment_id,
                        payload,
                        RunStatus.PLANNED.value,
                        max_attempts,
                        timestamp,
                        timestamp,
                        timestamp,
                    ),
                )
                connection.execute(
                    "INSERT INTO run_events(run_id, from_status, to_status, occurred_at, "
                    "revision, detail) VALUES(?, NULL, ?, ?, 0, ?)",
                    (job.run_id, RunStatus.PLANNED.value, timestamp, "final test activated"),
                )
                inserted += 1
            connection.execute(
                "UPDATE final_test_grants SET consumed_at = ? WHERE grant_id = ? "
                "AND consumed_at IS NULL",
                (timestamp, grant_id),
            )
            updated = connection.execute(
                "SELECT * FROM final_test_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
            required = _require_row(updated, operation="final-test grant consumption")
            return self._grant(required), inserted

    def get_final_test_grant(self, grant_id: str) -> FinalTestGrant:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM final_test_grants WHERE grant_id = ?", (grant_id,)
            ).fetchone()
        if row is None:
            raise FinalTestAccessError("final test grant does not exist")
        return self._grant(row)

    def mark_corrupted(self, run_id: str, *, detail: str, at: datetime) -> RunState:
        with self._transaction() as connection:
            row = self._transition(
                connection,
                run_id=run_id,
                target=RunStatus.CORRUPTED,
                at=at,
                detail=detail,
            )
            return self._state(row)

    def get_job(self, run_id: str) -> BacktestJob:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT job_json FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise StateRepositoryError("run is not registered")
        return _job(row["job_json"])

    def get_run(self, run_id: str) -> RunState:
        with self._reader() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if row is None:
            raise StateRepositoryError("run is not registered")
        return self._state(row)

    def list_runs(
        self, experiment_id: str, *, statuses: Iterable[RunStatus] | None = None
    ) -> tuple[RunState, ...]:
        parameters: list[Any] = [experiment_id]
        query = "SELECT * FROM runs WHERE experiment_id = ?"
        if statuses is not None:
            values = tuple(sorted({item.value for item in statuses}))
            if not values:
                return ()
            query += " AND status IN (" + ",".join("?" for _ in values) + ")"
            parameters.extend(values)
        query += " ORDER BY run_id"
        with self._reader() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return tuple(self._state(row) for row in rows)

    def events(self, run_id: str) -> tuple[StateEvent, ...]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM run_events WHERE run_id = ? ORDER BY event_id", (run_id,)
            ).fetchall()
        return tuple(
            StateEvent(
                event_id=int(row["event_id"]),
                run_id=row["run_id"],
                from_status=(RunStatus(row["from_status"]) if row["from_status"] else None),
                to_status=RunStatus(row["to_status"]),
                occurred_at=_datetime(row["occurred_at"]),
                revision=int(row["revision"]),
                detail=row["detail"],
            )
            for row in rows
        )

    def next_available_at(self, experiment_id: str) -> datetime | None:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT MIN(available_at) AS value FROM runs WHERE experiment_id = ? "
                "AND status IN (?, ?)",
                (experiment_id, RunStatus.QUEUED.value, RunStatus.RETRY_WAIT.value),
            ).fetchone()
        if row is None or row["value"] is None:
            return None
        return _datetime(row["value"])

    def has_nonterminal_runs(self, experiment_id: str) -> bool:
        with self._reader() as connection:
            row = connection.execute(
                "SELECT 1 FROM runs WHERE experiment_id = ? AND status IN (?, ?, ?, ?) LIMIT 1",
                (
                    experiment_id,
                    RunStatus.PLANNED.value,
                    RunStatus.QUEUED.value,
                    RunStatus.RUNNING.value,
                    RunStatus.RETRY_WAIT.value,
                ),
            ).fetchone()
        return row is not None

    def counts(self, experiment_id: str) -> Mapping[RunStatus, int]:
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT status, COUNT(*) AS count FROM runs WHERE experiment_id = ? "
                "GROUP BY status",
                (experiment_id,),
            ).fetchall()
        return {RunStatus(row["status"]): int(row["count"]) for row in rows}

    @staticmethod
    def _grant(row: sqlite3.Row) -> FinalTestGrant:
        candidates = canonical_json_loads(row["candidates_json"])
        if not isinstance(candidates, tuple) or any(
            not isinstance(item, str) for item in candidates
        ):
            raise StateRepositoryError("stored final test candidates are invalid")
        return FinalTestGrant(
            grant_id=row["grant_id"],
            experiment_id=row["experiment_id"],
            specification_sha256=row["specification_sha256"],
            split_fingerprint=row["split_fingerprint"],
            candidate_configuration_hashes=candidates,
            issued_at=_datetime(row["issued_at"]),
            consumed_at=(_datetime(row["consumed_at"]) if row["consumed_at"] is not None else None),
        )

    @staticmethod
    def _state(row: sqlite3.Row) -> RunState:
        return RunState(
            run_id=row["run_id"],
            experiment_id=row["experiment_id"],
            status=RunStatus(row["status"]),
            attempt_count=int(row["attempt_count"]),
            max_attempts=int(row["max_attempts"]),
            created_at=_datetime(row["created_at"]),
            updated_at=_datetime(row["updated_at"]),
            available_at=_datetime(row["available_at"]),
            revision=int(row["revision"]),
            lease_owner=row["lease_owner"],
            lease_expires_at=(
                _datetime(row["lease_expires_at"]) if row["lease_expires_at"] else None
            ),
            failure=_failure(row["failure_json"]),
            result=_result(row["result_json"]),
        )
