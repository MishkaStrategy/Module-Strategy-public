from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    AcceptanceCheckpoint,
    AcceptanceErrorCode,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceRunSnapshot,
    AcceptanceRunSpec,
    AcceptanceStatus,
    EvidenceReference,
    ResourceSample,
)

_SCHEMA_VERSION = 1
_MAX_DB_BYTES = 1 << 30


@dataclass(frozen=True, slots=True)
class StoredRun:
    snapshot: AcceptanceRunSnapshot
    phase_attempt: int
    phase_attempt_id: str | None
    phase_execution: str
    lease_owner: str | None
    lease_expires_at: datetime | None
    stop_requested: bool
    cancel_requested: bool


class SQLiteAcceptanceStore:
    """SQLite persistence owned by System Acceptance, not by Control Plane."""

    def __init__(self, path: str | Path) -> None:
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance store must not be a symlink",
            )
        self.path = raw
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._lock = threading.RLock()
            self._db = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA foreign_keys=ON")
            self._db.execute("PRAGMA busy_timeout=30000")
            self._db.execute("PRAGMA synchronous=FULL")
            self._migrate()
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance store initialization failed",
            ) from None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
            except BaseException:
                self._db.rollback()
                raise
            else:
                self._db.commit()

    def _migrate(self) -> None:
        with self._lock, self._db:
            self._db.executescript(
                """
                CREATE TABLE IF NOT EXISTS acceptance_schema_version(
                  version INTEGER PRIMARY KEY
                );
                INSERT OR IGNORE INTO acceptance_schema_version(version) VALUES(1);

                CREATE TABLE IF NOT EXISTS acceptance_runs(
                  run_id TEXT PRIMARY KEY,
                  payload TEXT NOT NULL,
                  status TEXT NOT NULL,
                  phase TEXT NOT NULL,
                  updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS acceptance_mutations(
                  request_id TEXT PRIMARY KEY,
                  fingerprint TEXT NOT NULL,
                  state TEXT NOT NULL,
                  result TEXT,
                  error_code TEXT,
                  created_at TEXT NOT NULL,
                  completed_at TEXT
                );

                CREATE TABLE IF NOT EXISTS acceptance_audit(
                  event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                  timestamp TEXT NOT NULL,
                  mutation TEXT NOT NULL,
                  request_id TEXT NOT NULL,
                  operator TEXT NOT NULL,
                  run_id TEXT NOT NULL,
                  result TEXT NOT NULL,
                  previous_status TEXT,
                  new_status TEXT,
                  phase TEXT NOT NULL
                );
                """
            )
            versions = tuple(
                int(row[0])
                for row in self._db.execute(
                    "SELECT version FROM acceptance_schema_version ORDER BY version"
                ).fetchall()
            )
            if versions != (_SCHEMA_VERSION,):
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.RECOVERY_REQUIRED,
                    "acceptance store schema is unsupported",
                )

    def load_run(
        self, run_id: str, *, connection: sqlite3.Connection | None = None
    ) -> StoredRun | None:
        db = connection or self._db
        try:
            row = db.execute(
                "SELECT payload FROM acceptance_runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            return _decode_run(row[0]) if row is not None else None
        except AcceptanceOwnerError:
            raise
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance run read failed",
                run_id=run_id,
            ) from None

    def latest_run(self) -> StoredRun | None:
        try:
            with self._lock:
                row = self._db.execute(
                    "SELECT payload FROM acceptance_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1"
                ).fetchone()
            return _decode_run(row[0]) if row is not None else None
        except AcceptanceOwnerError:
            raise
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance latest-run read failed",
            ) from None

    def active_run(self, *, connection: sqlite3.Connection | None = None) -> StoredRun | None:
        db = connection or self._db
        terminal = tuple(
            status.value
            for status in (
                AcceptanceStatus.SUCCEEDED,
                AcceptanceStatus.FAILED,
                AcceptanceStatus.CANCELLED,
                AcceptanceStatus.BLOCKED,
            )
        )
        try:
            rows = db.execute(
                "SELECT payload FROM acceptance_runs WHERE status NOT IN (?,?,?,?) "
                "ORDER BY updated_at DESC",
                terminal,
            ).fetchall()
            if len(rows) > 1:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.RECOVERY_REQUIRED,
                    "multiple active acceptance runs detected",
                )
            return _decode_run(rows[0][0]) if rows else None
        except AcceptanceOwnerError:
            raise
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance active-run read failed",
            ) from None

    def save_run(self, stored: StoredRun, *, connection: sqlite3.Connection) -> None:
        payload = _encode_run(stored)
        if len(payload.encode("utf-8")) > 256_000:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance run payload exceeds bounded size",
                run_id=stored.snapshot.acceptance_run_id,
            )
        snapshot = stored.snapshot
        try:
            connection.execute(
                "INSERT INTO acceptance_runs(run_id,payload,status,phase,updated_at) VALUES(?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET payload=excluded.payload,status=excluded.status,"
                "phase=excluded.phase,updated_at=excluded.updated_at",
                (
                    snapshot.acceptance_run_id,
                    payload,
                    snapshot.status.value,
                    snapshot.phase.value,
                    snapshot.updated_at.isoformat(),
                ),
            )
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance run write failed",
                run_id=snapshot.acceptance_run_id,
            ) from None

    def mutation_row(
        self,
        request_id: str,
        *,
        connection: sqlite3.Connection,
    ) -> tuple[str, str, str | None, str | None, str, str | None] | None:
        try:
            row = connection.execute(
                "SELECT fingerprint,state,result,error_code,created_at,completed_at "
                "FROM acceptance_mutations WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                return None
            return tuple(str(value) if value is not None else None for value in row)  # type: ignore[return-value]
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance mutation read failed",
                request_id=request_id,
            ) from None

    def insert_mutation(
        self,
        *,
        request_id: str,
        fingerprint: str,
        created_at: datetime,
        connection: sqlite3.Connection,
    ) -> None:
        try:
            connection.execute(
                "INSERT INTO acceptance_mutations(request_id,fingerprint,state,result,error_code,"
                "created_at,completed_at) VALUES(?,?,?,?,?,?,?)",
                (request_id, fingerprint, "IN_PROGRESS", None, None, created_at.isoformat(), None),
            )
        except sqlite3.IntegrityError:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.IDEMPOTENCY_CONFLICT,
                "acceptance mutation request_id already exists",
                request_id=request_id,
            ) from None
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance mutation claim failed",
                request_id=request_id,
            ) from None

    def finish_mutation(
        self,
        *,
        request_id: str,
        result: Mapping[str, object] | None,
        error_code: AcceptanceErrorCode | None,
        completed_at: datetime,
        connection: sqlite3.Connection,
    ) -> None:
        state = "SUCCEEDED" if error_code is None else "FAILED"
        encoded_result = (
            json.dumps(dict(result), sort_keys=True, separators=(",", ":"))
            if result is not None
            else None
        )
        transaction_was_active = connection.in_transaction
        try:
            cursor = connection.execute(
                "UPDATE acceptance_mutations SET state=?,result=?,error_code=?,completed_at=? "
                "WHERE request_id=? AND state='IN_PROGRESS'",
                (
                    state,
                    encoded_result,
                    error_code.value if error_code is not None else None,
                    completed_at.isoformat(),
                    request_id,
                ),
            )
            if cursor.rowcount != 1:
                if error_code is AcceptanceErrorCode.STORAGE_FAILURE and not transaction_was_active:
                    raise AcceptanceOwnerError(
                        AcceptanceErrorCode.STORAGE_FAILURE,
                        "acceptance mutation completion failed",
                        request_id=request_id,
                    )
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.RECOVERY_REQUIRED,
                    "acceptance mutation completion is inconsistent",
                    request_id=request_id,
                )
        except AcceptanceOwnerError:
            raise
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance mutation completion failed",
                request_id=request_id,
            ) from None

    def append_audit(
        self,
        *,
        timestamp: datetime,
        mutation: str,
        request_id: str,
        operator: str,
        run_id: str,
        result: str,
        previous_status: str | None,
        new_status: str | None,
        phase: str,
        connection: sqlite3.Connection,
    ) -> None:
        values = (mutation, request_id, operator, run_id, result, phase)
        if any(not isinstance(value, str) or len(value) > 128 for value in values):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "acceptance audit value is invalid",
                request_id=request_id,
                run_id=run_id,
            )
        try:
            connection.execute(
                "INSERT INTO acceptance_audit(timestamp,mutation,request_id,operator,run_id,result,"
                "previous_status,new_status,phase) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    timestamp.isoformat(),
                    mutation,
                    request_id,
                    operator,
                    run_id,
                    result,
                    previous_status,
                    new_status,
                    phase,
                ),
            )
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance audit write failed",
                request_id=request_id,
                run_id=run_id,
            ) from None

    def close(self) -> None:
        with self._lock:
            try:
                self._db.close()
            except Exception:
                pass

    def verify_storage_bound(self) -> None:
        try:
            if self.path.exists() and self.path.stat().st_size > _MAX_DB_BYTES:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.STORAGE_FAILURE,
                    "acceptance store exceeds bounded size",
                )
        except AcceptanceOwnerError:
            raise
        except Exception:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.STORAGE_FAILURE,
                "acceptance store size check failed",
            ) from None


def _encode_run(stored: StoredRun) -> str:
    snapshot = stored.snapshot
    payload = {
        "schema_version": _SCHEMA_VERSION,
        "snapshot": {
            "acceptance_run_id": snapshot.acceptance_run_id,
            "spec": _spec_dict(snapshot.spec),
            "status": snapshot.status.value,
            "phase": snapshot.phase.value,
            "created_at": snapshot.created_at.isoformat(),
            "started_at": _time(snapshot.started_at),
            "updated_at": snapshot.updated_at.isoformat(),
            "completed_at": _time(snapshot.completed_at),
            "active_test_time": snapshot.active_test_time,
            "wall_clock_elapsed": snapshot.wall_clock_elapsed,
            "restart_count": snapshot.restart_count,
            "checkpoint": _checkpoint_dict(snapshot.checkpoint),
            "resources": [_resource_dict(item) for item in snapshot.resources],
            "warnings": list(snapshot.warnings),
            "error": snapshot.error,
            "next_action": snapshot.next_action,
            "evidence": [_evidence_dict(item) for item in snapshot.evidence],
        },
        "phase_attempt": stored.phase_attempt,
        "phase_attempt_id": stored.phase_attempt_id,
        "phase_execution": stored.phase_execution,
        "lease_owner": stored.lease_owner,
        "lease_expires_at": _time(stored.lease_expires_at),
        "stop_requested": stored.stop_requested,
        "cancel_requested": stored.cancel_requested,
    }
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _decode_run(raw: object) -> StoredRun:
    try:
        if not isinstance(raw, str):
            raise ValueError("payload is not text")
        value = json.loads(raw)
        if not isinstance(value, dict) or value.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("schema mismatch")
        snapshot_raw = value["snapshot"]
        if not isinstance(snapshot_raw, dict):
            raise ValueError("snapshot is not object")
        snapshot = AcceptanceRunSnapshot(
            acceptance_run_id=str(snapshot_raw["acceptance_run_id"]),
            spec=_spec(snapshot_raw["spec"]),
            status=AcceptanceStatus(str(snapshot_raw["status"])),
            phase=AcceptancePhase(str(snapshot_raw["phase"])),
            created_at=_parse_time(snapshot_raw["created_at"]),
            started_at=_optional_time(snapshot_raw.get("started_at")),
            updated_at=_parse_time(snapshot_raw["updated_at"]),
            completed_at=_optional_time(snapshot_raw.get("completed_at")),
            active_test_time=float(snapshot_raw["active_test_time"]),
            wall_clock_elapsed=float(snapshot_raw["wall_clock_elapsed"]),
            restart_count=int(snapshot_raw["restart_count"]),
            checkpoint=_checkpoint(snapshot_raw.get("checkpoint")),
            resources=tuple(_resource(item) for item in _list(snapshot_raw.get("resources"))),
            warnings=tuple(str(item) for item in _list(snapshot_raw.get("warnings"))),
            error=_optional_str(snapshot_raw.get("error")),
            next_action=_optional_str(snapshot_raw.get("next_action")),
            evidence=tuple(_evidence(item) for item in _list(snapshot_raw.get("evidence"))),
        )
        phase_attempt = int(value["phase_attempt"])
        phase_attempt_id = _optional_str(value.get("phase_attempt_id"))
        phase_execution = str(value["phase_execution"])
        if phase_execution not in {"IDLE", "IN_PROGRESS"}:
            raise ValueError("phase_execution invalid")
        lease_owner = _optional_str(value.get("lease_owner"))
        lease_expires_at = _optional_time(value.get("lease_expires_at"))
        stop_requested = value["stop_requested"]
        cancel_requested = value["cancel_requested"]
        if (
            phase_attempt < 0
            or not isinstance(stop_requested, bool)
            or not isinstance(cancel_requested, bool)
        ):
            raise ValueError("internal run metadata invalid")
        if phase_execution == "IN_PROGRESS" and (
            phase_attempt_id is None or lease_owner is None or lease_expires_at is None
        ):
            raise ValueError("in-progress phase lease incomplete")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, AcceptanceOwnerError):
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "persisted acceptance run is invalid",
        ) from None
    return StoredRun(
        snapshot=snapshot,
        phase_attempt=phase_attempt,
        phase_attempt_id=phase_attempt_id,
        phase_execution=phase_execution,
        lease_owner=lease_owner,
        lease_expires_at=lease_expires_at,
        stop_requested=stop_requested,
        cancel_requested=cancel_requested,
    )


def _spec_dict(spec: AcceptanceRunSpec) -> dict[str, object]:
    return {
        "full_rc_sha": spec.full_rc_sha,
        "dataset_id": spec.dataset_id,
        "dataset_digest": spec.dataset_digest,
        "strategy_id": spec.strategy_id,
        "strategy_digest": spec.strategy_digest,
        "config_digest": spec.config_digest,
        "enable_soak_24h": spec.enable_soak_24h,
        "enable_soak_72h": spec.enable_soak_72h,
        "mode": spec.mode,
    }


def _spec(raw: object) -> AcceptanceRunSpec:
    if not isinstance(raw, dict):
        raise ValueError("spec is not object")
    return AcceptanceRunSpec(
        full_rc_sha=str(raw["full_rc_sha"]),
        dataset_id=str(raw["dataset_id"]),
        dataset_digest=str(raw["dataset_digest"]),
        strategy_id=str(raw["strategy_id"]),
        strategy_digest=str(raw["strategy_digest"]),
        config_digest=str(raw["config_digest"]),
        enable_soak_24h=raw["enable_soak_24h"],
        enable_soak_72h=raw["enable_soak_72h"],
        mode=str(raw["mode"]),
    )


def _checkpoint_dict(value: AcceptanceCheckpoint | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "phase": value.phase.value,
        "updated_at": value.updated_at.isoformat(),
        "token": value.token,
        "digest": value.digest,
    }


def _checkpoint(raw: object) -> AcceptanceCheckpoint | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("checkpoint is not object")
    return AcceptanceCheckpoint(
        phase=AcceptancePhase(str(raw["phase"])),
        updated_at=_parse_time(raw["updated_at"]),
        token=str(raw["token"]),
        digest=str(raw["digest"]),
    )


def _resource_dict(value: ResourceSample) -> dict[str, object]:
    return {
        "timestamp": value.timestamp.isoformat(),
        "cpu_percent": value.cpu_percent,
        "ram_bytes": value.ram_bytes,
        "disk_free_bytes": value.disk_free_bytes,
    }


def _resource(raw: object) -> ResourceSample:
    if not isinstance(raw, dict):
        raise ValueError("resource is not object")
    return ResourceSample(
        timestamp=_parse_time(raw["timestamp"]),
        cpu_percent=float(raw["cpu_percent"]),
        ram_bytes=int(raw["ram_bytes"]),
        disk_free_bytes=int(raw["disk_free_bytes"]),
    )


def _evidence_dict(value: EvidenceReference) -> dict[str, object]:
    return {"kind": value.kind, "digest": value.digest, "reference": value.reference}


def _evidence(raw: object) -> EvidenceReference:
    if not isinstance(raw, dict):
        raise ValueError("evidence is not object")
    return EvidenceReference(
        kind=str(raw["kind"]),
        digest=str(raw["digest"]),
        reference=str(raw["reference"]),
    )


def _parse_time(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp is not text")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(UTC)


def _optional_time(value: object) -> datetime | None:
    return None if value is None else _parse_time(value)


def _time(value: datetime | None) -> str | None:
    return value.astimezone(UTC).isoformat() if value is not None else None


def _list(value: object) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError("value is not list")
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("value is not string")
    return value
