from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .canonical import canonical_json_loads
from .models import ExperimentStatus
from .repository import SqliteExperimentRepository
from .state import FailureCategory, RunStatus


class ReadOnlyRepositoryError(RuntimeError):
    """Raised when durable Research state cannot be safely projected."""


class ReadOnlyRepositoryNotFound(ReadOnlyRepositoryError):
    """Raised when a requested experiment or run is absent."""


@dataclass(frozen=True, slots=True)
class ResearchExperimentSnapshot:
    experiment_id: str
    specification_sha256: str
    status: ExperimentStatus
    created_at: datetime
    updated_at: datetime
    run_counts: Mapping[RunStatus, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "experiment_id", _identifier(self.experiment_id, "experiment_id"))
        object.__setattr__(self, "specification_sha256", _sha256(self.specification_sha256))
        object.__setattr__(self, "created_at", _aware(self.created_at, "created_at"))
        object.__setattr__(self, "updated_at", _aware(self.updated_at, "updated_at"))
        counts: dict[RunStatus, int] = {}
        for status, count in self.run_counts.items():
            if not isinstance(status, RunStatus):
                raise ReadOnlyRepositoryError("run count status is invalid")
            counts[status] = _non_negative_int(count, "run count")
        object.__setattr__(self, "run_counts", MappingProxyType(counts))


@dataclass(frozen=True, slots=True)
class ResearchRunSnapshot:
    run_id: str
    experiment_id: str
    status: RunStatus
    stage_id: str
    split_id: str
    dataset_id: str
    scenario_id: str
    stage_role: str
    repetition_index: int
    attempt_count: int
    max_attempts: int
    created_at: datetime
    updated_at: datetime
    available_at: datetime
    revision: int
    lease_expires_at: datetime | None
    failure_category: FailureCategory | None
    failure_retryable: bool | None
    engine_run_id: str | None
    bars_processed: int | None
    restart_verified: bool | None

    def __post_init__(self) -> None:
        for name in (
            "run_id",
            "experiment_id",
            "stage_id",
            "split_id",
            "dataset_id",
            "scenario_id",
            "stage_role",
        ):
            object.__setattr__(self, name, _identifier(getattr(self, name), name))
        _non_negative_int(self.repetition_index, "repetition_index")
        _non_negative_int(self.attempt_count, "attempt_count")
        _positive_int(self.max_attempts, "max_attempts")
        if self.attempt_count > self.max_attempts:
            raise ReadOnlyRepositoryError("attempt counters are invalid")
        _non_negative_int(self.revision, "revision")
        for name in ("created_at", "updated_at", "available_at"):
            object.__setattr__(self, name, _aware(getattr(self, name), name))
        if self.lease_expires_at is not None:
            object.__setattr__(
                self,
                "lease_expires_at",
                _aware(self.lease_expires_at, "lease_expires_at"),
            )
        if self.status is RunStatus.RUNNING and self.lease_expires_at is None:
            raise ReadOnlyRepositoryError("running run lease expiry is missing")
        if self.status is not RunStatus.RUNNING and self.lease_expires_at is not None:
            raise ReadOnlyRepositoryError("only running runs may expose lease expiry")
        if (self.failure_category is None) != (self.failure_retryable is None):
            raise ReadOnlyRepositoryError("failure projection is incomplete")
        if self.failure_retryable is not None and not isinstance(self.failure_retryable, bool):
            raise ReadOnlyRepositoryError("failure_retryable is invalid")
        if self.engine_run_id is not None:
            object.__setattr__(
                self, "engine_run_id", _identifier(self.engine_run_id, "engine_run_id")
            )
        if self.bars_processed is not None:
            _non_negative_int(self.bars_processed, "bars_processed")
        if self.restart_verified is not None and not isinstance(self.restart_verified, bool):
            raise ReadOnlyRepositoryError("restart_verified is invalid")
        has_complete_result = (
            self.engine_run_id is not None
            and self.bars_processed is not None
            and self.restart_verified is not None
        )
        if self.status is RunStatus.SUCCEEDED and not has_complete_result:
            raise ReadOnlyRepositoryError("successful run result identity is incomplete")
        if self.status is not RunStatus.SUCCEEDED and any(
            value is not None
            for value in (self.engine_run_id, self.bars_processed, self.restart_verified)
        ):
            raise ReadOnlyRepositoryError("only successful runs may expose result identity")


class ReadOnlyExperimentRepository:
    """Non-mutating query port over an existing Research Runner database.

    This class never constructs the mutating repository. Connections use SQLite ``mode=ro`` and
    ``query_only``. Therefore a Control UI read cannot create or migrate schema, claim leases, or
    change experiment state.
    """

    SCHEMA_VERSION = SqliteExperimentRepository.SCHEMA_VERSION
    _REQUIRED_COLUMNS: Mapping[str, frozenset[str]] = MappingProxyType(
        {
            "repository_meta": frozenset({"key", "value"}),
            "experiments": frozenset(
                {
                    "experiment_id",
                    "specification_sha256",
                    "manifest_json",
                    "status",
                    "created_at",
                    "updated_at",
                }
            ),
            "runs": frozenset(
                {
                    "run_id",
                    "experiment_id",
                    "job_json",
                    "status",
                    "attempt_count",
                    "max_attempts",
                    "available_at",
                    "lease_owner",
                    "lease_expires_at",
                    "failure_json",
                    "result_json",
                    "created_at",
                    "updated_at",
                    "revision",
                }
            ),
            "run_claims": frozenset(
                {"run_id", "worker_id", "session_id", "lease_token", "claim_revision"}
            ),
        }
    )

    def __init__(self, path: str | Path) -> None:
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise ReadOnlyRepositoryError("Research state database must not be a symlink")
        if not raw.exists():
            raise ReadOnlyRepositoryError("Research state database does not exist")
        if not raw.is_file():
            raise ReadOnlyRepositoryError("Research state database must be a regular file")
        self.path = raw.resolve(strict=True)
        self._uri = f"{self.path.as_uri()}?mode=ro"
        self._validate_schema()

    @contextmanager
    def _reader(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = sqlite3.connect(
                self._uri,
                uri=True,
                timeout=5.0,
                isolation_level=None,
            )
        except sqlite3.Error as exc:
            raise ReadOnlyRepositoryError(
                "Research state database cannot be opened read-only"
            ) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error as exc:
            raise ReadOnlyRepositoryError("Research state database read failed") from exc
        finally:
            connection.close()

    def _validate_schema(self) -> None:
        with self._reader() as connection:
            tables = {
                _required_text(row["name"], "table name")
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            for table, required in self._REQUIRED_COLUMNS.items():
                if table not in tables:
                    raise ReadOnlyRepositoryError(f"Research state table is missing: {table}")
                columns = {
                    _required_text(row["name"], "column name")
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not required <= columns:
                    raise ReadOnlyRepositoryError(
                        f"Research state table has an incompatible schema: {table}"
                    )
            row = connection.execute(
                "SELECT value FROM repository_meta WHERE key = 'schema_version'"
            ).fetchone()
            if row is None:
                raise ReadOnlyRepositoryError("Research state schema version is missing")
            try:
                version = int(row["value"])
            except (TypeError, ValueError) as exc:
                raise ReadOnlyRepositoryError("Research state schema version is invalid") from exc
            if version != self.SCHEMA_VERSION:
                raise ReadOnlyRepositoryError("unsupported Research state schema version")

    def list_experiments(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[ResearchExperimentSnapshot, ...]:
        _bounded_page(limit=limit, offset=offset)
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT experiment_id, specification_sha256, status, created_at, updated_at "
                "FROM experiments ORDER BY updated_at DESC, experiment_id DESC LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return tuple(self._experiment(connection, row) for row in rows)

    def get_experiment(self, experiment_id: str) -> ResearchExperimentSnapshot:
        safe_id = _identifier(experiment_id, "experiment_id")
        with self._reader() as connection:
            row = connection.execute(
                "SELECT experiment_id, specification_sha256, status, created_at, updated_at "
                "FROM experiments WHERE experiment_id = ?",
                (safe_id,),
            ).fetchone()
            if row is None:
                raise ReadOnlyRepositoryNotFound("experiment is not registered")
            return self._experiment(connection, row)

    def list_runs(
        self,
        experiment_id: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[ResearchRunSnapshot, ...]:
        safe_id = _identifier(experiment_id, "experiment_id")
        _bounded_page(limit=limit, offset=offset)
        with self._reader() as connection:
            exists = connection.execute(
                "SELECT 1 FROM experiments WHERE experiment_id = ?",
                (safe_id,),
            ).fetchone()
            if exists is None:
                raise ReadOnlyRepositoryNotFound("experiment is not registered")
            rows = connection.execute(
                "SELECT * FROM runs WHERE experiment_id = ? "
                "ORDER BY updated_at DESC, run_id DESC LIMIT ? OFFSET ?",
                (safe_id, limit, offset),
            ).fetchall()
        return tuple(self._run(row) for row in rows)

    def get_run(self, run_id: str) -> ResearchRunSnapshot:
        safe_id = _identifier(run_id, "run_id")
        with self._reader() as connection:
            row = connection.execute("SELECT * FROM runs WHERE run_id = ?", (safe_id,)).fetchone()
        if row is None:
            raise ReadOnlyRepositoryNotFound("run is not registered")
        return self._run(row)

    @staticmethod
    def _experiment(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> ResearchExperimentSnapshot:
        try:
            status = ExperimentStatus(_required_text(row["status"], "experiment status"))
        except ValueError as exc:
            raise ReadOnlyRepositoryError("stored experiment status is invalid") from exc
        count_rows = connection.execute(
            "SELECT status, COUNT(*) AS count FROM runs WHERE experiment_id = ? GROUP BY status",
            (row["experiment_id"],),
        ).fetchall()
        counts: dict[RunStatus, int] = {}
        for count_row in count_rows:
            try:
                run_status = RunStatus(_required_text(count_row["status"], "run status"))
            except ValueError as exc:
                raise ReadOnlyRepositoryError("stored run status is invalid") from exc
            counts[run_status] = _non_negative_int(count_row["count"], "run count")
        return ResearchExperimentSnapshot(
            experiment_id=_required_text(row["experiment_id"], "experiment_id"),
            specification_sha256=_required_text(
                row["specification_sha256"], "specification_sha256"
            ),
            status=status,
            created_at=_datetime_text(
                _required_text(row["created_at"], "created_at"), "created_at"
            ),
            updated_at=_datetime_text(
                _required_text(row["updated_at"], "updated_at"), "updated_at"
            ),
            run_counts=counts,
        )

    @staticmethod
    def _run(row: sqlite3.Row) -> ResearchRunSnapshot:
        run_id = _required_text(row["run_id"], "run_id")
        experiment_id = _required_text(row["experiment_id"], "experiment_id")
        job = _object_json(row["job_json"], "job_json")
        if _mapping_text(job, "run_id") != run_id:
            raise ReadOnlyRepositoryError("stored job run identity mismatch")
        if _mapping_text(job, "experiment_id") != experiment_id:
            raise ReadOnlyRepositoryError("stored job experiment identity mismatch")
        try:
            status = RunStatus(_required_text(row["status"], "run status"))
        except ValueError as exc:
            raise ReadOnlyRepositoryError("stored run status is invalid") from exc

        failure_category: FailureCategory | None = None
        failure_retryable: bool | None = None
        if row["failure_json"] is not None:
            failure = _object_json(row["failure_json"], "failure_json")
            try:
                failure_category = FailureCategory(_mapping_text(failure, "category"))
            except ValueError as exc:
                raise ReadOnlyRepositoryError("stored failure category is invalid") from exc
            retryable = failure.get("retryable")
            if not isinstance(retryable, bool):
                raise ReadOnlyRepositoryError("stored failure retryable flag is invalid")
            failure_retryable = retryable

        engine_run_id: str | None = None
        bars_processed: int | None = None
        restart_verified: bool | None = None
        if row["result_json"] is not None:
            result = _object_json(row["result_json"], "result_json")
            if _mapping_text(result, "requested_run_id") != run_id:
                raise ReadOnlyRepositoryError("stored result run identity mismatch")
            engine_run_id = _mapping_text(result, "engine_run_id")
            bars_processed = _non_negative_int(result.get("bars_processed"), "bars_processed")
            raw_restart = result.get("restart_verified")
            if not isinstance(raw_restart, bool):
                raise ReadOnlyRepositoryError("stored result restart_verified is invalid")
            restart_verified = raw_restart

        return ResearchRunSnapshot(
            run_id=run_id,
            experiment_id=experiment_id,
            status=status,
            stage_id=_mapping_text(job, "stage_id"),
            split_id=_mapping_text(job, "split_id"),
            dataset_id=_mapping_text(job, "dataset_id"),
            scenario_id=_mapping_text(job, "scenario_id"),
            stage_role=_optional_mapping_text(job, "stage_role", default="unspecified"),
            repetition_index=_non_negative_int(job.get("repetition_index", 0), "repetition_index"),
            attempt_count=_non_negative_int(row["attempt_count"], "attempt_count"),
            max_attempts=_positive_int(row["max_attempts"], "max_attempts"),
            created_at=_datetime_text(
                _required_text(row["created_at"], "created_at"), "created_at"
            ),
            updated_at=_datetime_text(
                _required_text(row["updated_at"], "updated_at"), "updated_at"
            ),
            available_at=_datetime_text(
                _required_text(row["available_at"], "available_at"), "available_at"
            ),
            revision=_non_negative_int(row["revision"], "revision"),
            lease_expires_at=(
                _datetime_text(
                    _required_text(row["lease_expires_at"], "lease_expires_at"),
                    "lease_expires_at",
                )
                if row["lease_expires_at"] is not None
                else None
            ),
            failure_category=failure_category,
            failure_retryable=failure_retryable,
            engine_run_id=engine_run_id,
            bars_processed=bars_processed,
            restart_verified=restart_verified,
        )


def _bounded_page(*, limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be non-negative")


def _required_text(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ReadOnlyRepositoryError(f"{name} is invalid")
    return _identifier(value, name)


def _mapping_text(value: Mapping[str, Any], key: str) -> str:
    if key not in value:
        raise ReadOnlyRepositoryError(f"stored {key} is missing")
    return _required_text(value[key], key)


def _optional_mapping_text(value: Mapping[str, Any], key: str, *, default: str) -> str:
    if key not in value:
        return _identifier(default, key)
    return _required_text(value[key], key)


def _identifier(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        raise ReadOnlyRepositoryError(f"{name} is invalid")
    if any(character in normalized for character in ("\x00", "\n", "\r")):
        raise ReadOnlyRepositoryError(f"{name} is invalid")
    return normalized


def _sha256(value: str) -> str:
    normalized = value.strip()
    if len(normalized) != 64 or any(
        character not in "0123456789abcdef" for character in normalized
    ):
        raise ReadOnlyRepositoryError("specification_sha256 is invalid")
    return normalized


def _datetime_text(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReadOnlyRepositoryError(f"{name} is invalid") from exc
    return _aware(parsed, name)


def _aware(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ReadOnlyRepositoryError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _object_json(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, str):
        raise ReadOnlyRepositoryError(f"{name} is invalid")
    try:
        decoded = canonical_json_loads(value)
    except Exception as exc:
        raise ReadOnlyRepositoryError(f"{name} is invalid") from exc
    if not isinstance(decoded, dict):
        raise ReadOnlyRepositoryError(f"{name} must be an object")
    return decoded


def _non_negative_int(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReadOnlyRepositoryError(f"{name} is invalid")
    return value


def _positive_int(value: Any, name: str) -> int:
    result = _non_negative_int(value, name)
    if result < 1:
        raise ReadOnlyRepositoryError(f"{name} must be positive")
    return result
