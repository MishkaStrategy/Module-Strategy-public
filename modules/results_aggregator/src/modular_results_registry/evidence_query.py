"""Side-effect-free evidence index over an existing Results Aggregator registry."""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .canonical import canonical_sha256
from .errors import IntegrityError, RegistryError
from .model_common import EvidenceStatus, Severity
from .runtime_capabilities import require_posix_storage_capabilities
from .storage_common import (
    _SCHEMA_VERSION,
    _decode_canonical_json_object,
    _validate_sqlite_sidecars,
)
from .storage_scalars import strict_schema_version_text, strict_sqlite_integer

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_VERIFY_ROWS = 10_000


class ReadOnlyEvidenceRegistryError(RuntimeError):
    """Raised when immutable registry evidence cannot be safely projected."""


class ReadOnlyEvidenceNotFound(ReadOnlyEvidenceRegistryError):
    """Raised when a requested evidence record is absent."""


@dataclass(frozen=True, slots=True)
class EvidenceRegistryIntegritySnapshot:
    schema_version: int
    registry_revision: int
    evidence_count: int
    evidence_rows_verified: bool = True


@dataclass(frozen=True, slots=True)
class RegistryEvidenceSnapshot:
    evidence_id: str
    experiment_id: str
    evidence_type: str
    producer: str
    producer_version: str
    status: EvidenceStatus
    severity: Severity
    created_at: datetime
    evidence_hash: str
    content_hash: str
    registered_revision: int
    run_count: int
    artifact_count: int
    limitations_count: int


class ReadOnlyEvidenceRegistry:
    """Read immutable evidence metadata from an existing quiescent registry.

    Active SQLite sidecars fail closed. The immutable SQLite URI prevents the observer from
    creating WAL/SHM files or taking write-capable locks while reading the owner database.
    """

    def __init__(self, path: str | Path) -> None:
        require_posix_storage_capabilities()
        raw = Path(path).expanduser()
        if raw.is_symlink():
            raise ReadOnlyEvidenceRegistryError("results registry must not be a symlink")
        if not raw.exists():
            raise ReadOnlyEvidenceRegistryError("results registry does not exist")
        if not raw.is_file():
            raise ReadOnlyEvidenceRegistryError("results registry must be a regular file")
        try:
            _validate_sqlite_sidecars(raw)
        except IntegrityError as exc:
            raise ReadOnlyEvidenceRegistryError(
                "results registry sidecar validation failed"
            ) from exc
        _reject_sqlite_sidecars(raw)
        self.path = raw.resolve(strict=True)
        self._uri = f"{self.path.as_uri()}?mode=ro&immutable=1"
        self._validate_schema()
        self.verify_integrity()

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
            raise ReadOnlyEvidenceRegistryError(
                "results registry cannot be opened read-only"
            ) from exc
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error as exc:
            raise ReadOnlyEvidenceRegistryError("results registry read failed") from exc
        finally:
            connection.close()

    def _validate_schema(self) -> None:
        required = {
            "schema_meta": {"key", "value"},
            "registry_state": {"singleton", "revision"},
            "evidence": {
                "evidence_id",
                "experiment_id",
                "evidence_type",
                "status",
                "content_hash",
                "payload_json",
                "registered_revision",
            },
            "evidence_runs": {"evidence_id", "run_id"},
            "evidence_artifacts": {"evidence_id", "artifact_id"},
        }
        with self._reader() as connection:
            tables = {
                str(row["name"])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            for table, columns in required.items():
                if table not in tables:
                    raise ReadOnlyEvidenceRegistryError(
                        f"results registry table is missing: {table}"
                    )
                actual = {
                    str(row["name"])
                    for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
                }
                if not columns <= actual:
                    raise ReadOnlyEvidenceRegistryError(
                        f"results registry table schema is incompatible: {table}"
                    )
            row = connection.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            if row is None:
                raise ReadOnlyEvidenceRegistryError("results registry schema version is missing")
            try:
                version = strict_schema_version_text(row["value"])
            except (RegistryError, ValueError) as exc:
                raise ReadOnlyEvidenceRegistryError(
                    "results registry schema version is invalid"
                ) from exc
            if version != _SCHEMA_VERSION:
                raise ReadOnlyEvidenceRegistryError(
                    "unsupported Results Aggregator registry schema version"
                )

    def verify_integrity(self) -> EvidenceRegistryIntegritySnapshot:
        with self._reader() as connection:
            count_row = connection.execute("SELECT COUNT(*) AS count FROM evidence").fetchone()
            if count_row is None:
                raise ReadOnlyEvidenceRegistryError("evidence count query returned no row")
            evidence_count = _non_negative_integer(count_row["count"], "evidence count")
            if evidence_count > _MAX_VERIFY_ROWS:
                raise ReadOnlyEvidenceRegistryError(
                    "results evidence exceeds read-only verification limit"
                )
            revision_row = connection.execute(
                "SELECT revision FROM registry_state WHERE singleton=1"
            ).fetchone()
            if revision_row is None:
                raise ReadOnlyEvidenceRegistryError("registry revision is missing")
            try:
                revision = strict_sqlite_integer(
                    revision_row["revision"], "registry revision", minimum=0
                )
            except (IntegrityError, ValueError) as exc:
                raise ReadOnlyEvidenceRegistryError("registry revision is invalid") from exc
            rows = connection.execute("SELECT * FROM evidence ORDER BY evidence_id").fetchall()
            for row in rows:
                self._verified_snapshot(connection, row)
            return EvidenceRegistryIntegritySnapshot(
                schema_version=_SCHEMA_VERSION,
                registry_revision=revision,
                evidence_count=evidence_count,
            )

    def list_evidence(
        self,
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[RegistryEvidenceSnapshot, ...]:
        _bounded_page(limit=limit, offset=offset)
        self.verify_integrity()
        with self._reader() as connection:
            rows = connection.execute(
                "SELECT * FROM evidence ORDER BY evidence_id LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return tuple(self._verified_snapshot(connection, row) for row in rows)

    def get_evidence(self, evidence_id: str) -> RegistryEvidenceSnapshot:
        safe_id = _identifier(evidence_id, "evidence_id")
        self.verify_integrity()
        with self._reader() as connection:
            row = connection.execute(
                "SELECT * FROM evidence WHERE evidence_id=?",
                (safe_id,),
            ).fetchone()
            if row is None:
                raise ReadOnlyEvidenceNotFound("results evidence does not exist")
            return self._verified_snapshot(connection, row)

    @staticmethod
    def _verified_snapshot(
        connection: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> RegistryEvidenceSnapshot:
        evidence_id = _identifier(row["evidence_id"], "evidence_id")
        experiment_id = _identifier(row["experiment_id"], "experiment_id")
        evidence_type = _text(row["evidence_type"], "evidence_type", maximum=256)
        content_hash = _digest(row["content_hash"], "content_hash")
        try:
            status = EvidenceStatus(str(row["status"]))
        except ValueError as exc:
            raise ReadOnlyEvidenceRegistryError("stored evidence status is invalid") from exc
        try:
            payload, _ = _decode_canonical_json_object(
                row["payload_json"],
                context="stored evidence payload",
            )
        except IntegrityError as exc:
            raise ReadOnlyEvidenceRegistryError("stored evidence payload is invalid") from exc
        if canonical_sha256(payload) != content_hash:
            raise ReadOnlyEvidenceRegistryError("stored evidence content hash mismatch")
        if payload.get("evidence_id") != evidence_id:
            raise ReadOnlyEvidenceRegistryError("stored evidence identity mismatch")
        if payload.get("experiment_id") != experiment_id:
            raise ReadOnlyEvidenceRegistryError("stored evidence experiment identity mismatch")
        if payload.get("evidence_type") != evidence_type:
            raise ReadOnlyEvidenceRegistryError("stored evidence type mismatch")
        if payload.get("status") != status.value:
            raise ReadOnlyEvidenceRegistryError("stored evidence status mismatch")
        producer = _text(payload.get("producer"), "producer", maximum=256)
        producer_version = _text(
            payload.get("producer_version"),
            "producer_version",
            maximum=128,
        )
        try:
            severity = Severity(str(payload.get("severity")))
        except ValueError as exc:
            raise ReadOnlyEvidenceRegistryError("stored evidence severity is invalid") from exc
        created_at = _utc_text(payload.get("created_at"), "created_at")
        evidence_hash = _digest(payload.get("evidence_hash"), "evidence_hash")
        run_ids = _identifier_sequence(payload.get("run_ids"), "run_ids")
        artifact_ids = _identifier_sequence(payload.get("artifact_ids"), "artifact_ids")
        limitations = payload.get("limitations", [])
        if not isinstance(limitations, list) or any(
            not isinstance(item, str) or not item.strip() for item in limitations
        ):
            raise ReadOnlyEvidenceRegistryError("stored evidence limitations are invalid")
        run_rows = connection.execute(
            "SELECT run_id FROM evidence_runs WHERE evidence_id=? ORDER BY run_id",
            (evidence_id,),
        ).fetchall()
        artifact_rows = connection.execute(
            "SELECT artifact_id FROM evidence_artifacts WHERE evidence_id=? ORDER BY artifact_id",
            (evidence_id,),
        ).fetchall()
        relation_runs = tuple(str(item["run_id"]) for item in run_rows)
        relation_artifacts = tuple(str(item["artifact_id"]) for item in artifact_rows)
        if tuple(sorted(run_ids)) != relation_runs:
            raise ReadOnlyEvidenceRegistryError("stored evidence run relations mismatch")
        if tuple(sorted(artifact_ids)) != relation_artifacts:
            raise ReadOnlyEvidenceRegistryError("stored evidence artifact relations mismatch")
        return RegistryEvidenceSnapshot(
            evidence_id=evidence_id,
            experiment_id=experiment_id,
            evidence_type=evidence_type,
            producer=producer,
            producer_version=producer_version,
            status=status,
            severity=severity,
            created_at=created_at,
            evidence_hash=evidence_hash,
            content_hash=content_hash,
            registered_revision=_non_negative_integer(
                row["registered_revision"], "registered_revision"
            ),
            run_count=len(run_ids),
            artifact_count=len(artifact_ids),
            limitations_count=len(limitations),
        )


def _bounded_page(*, limit: int, offset: int) -> None:
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise ValueError("offset must be non-negative")


def _reject_sqlite_sidecars(path: Path) -> None:
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() or sidecar.is_symlink():
            raise ReadOnlyEvidenceRegistryError(
                "results registry has active SQLite sidecars; read-only snapshot is not quiescent"
            )


def _identifier_sequence(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    result = tuple(_identifier(item, name) for item in value)
    if len(set(result)) != len(result):
        raise ReadOnlyEvidenceRegistryError(f"{name} contains duplicates")
    return result


def _identifier(value: object, name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > 192:
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    if any(character in value for character in ("\x00", "\n", "\r")):
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    return value


def _text(value: object, name: str, *, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    return value


def _digest(value: object, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    return value


def _utc_text(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    rendered = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(rendered)
    except ValueError as exc:
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReadOnlyEvidenceRegistryError(f"{name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _non_negative_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ReadOnlyEvidenceRegistryError(f"{name} is invalid")
    return value


__all__ = [
    "EvidenceRegistryIntegritySnapshot",
    "ReadOnlyEvidenceNotFound",
    "ReadOnlyEvidenceRegistry",
    "ReadOnlyEvidenceRegistryError",
    "RegistryEvidenceSnapshot",
]
