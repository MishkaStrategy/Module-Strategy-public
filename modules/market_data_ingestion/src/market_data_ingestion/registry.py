from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from .canonical import primitive
from .contracts import DatasetDescriptor, DatasetRegistration
from .errors import RegistryError


class SQLiteDatasetRegistry:
    """Minimal immutable dataset registry, intentionally not an experiment registry."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS datasets (
                    dataset_id TEXT NOT NULL,
                    version INTEGER NOT NULL CHECK(version > 0),
                    dataset_hash TEXT NOT NULL CHECK(length(dataset_hash) = 64),
                    descriptor_json TEXT NOT NULL,
                    registered_at TEXT NOT NULL,
                    PRIMARY KEY(dataset_id, version)
                )
                """
            )
            schema_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='datasets'"
            ).fetchone()
            schema_sql = str(schema_row[0]) if schema_row is not None else ""
            if "UNIQUE(dataset_hash)" in schema_sql.replace(" ", ""):
                connection.execute(
                    """
                    CREATE TABLE datasets_v2 (
                        dataset_id TEXT NOT NULL,
                        version INTEGER NOT NULL CHECK(version > 0),
                        dataset_hash TEXT NOT NULL CHECK(length(dataset_hash) = 64),
                        descriptor_json TEXT NOT NULL,
                        registered_at TEXT NOT NULL,
                        PRIMARY KEY(dataset_id, version)
                    )
                    """
                )
                connection.execute(
                    "INSERT INTO datasets_v2 SELECT dataset_id, version, dataset_hash, "
                    "descriptor_json, registered_at FROM datasets"
                )
                connection.execute("DROP TABLE datasets")
                connection.execute("ALTER TABLE datasets_v2 RENAME TO datasets")
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_datasets_hash ON datasets(dataset_hash)"
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS ingestion_requests (
                    request_id TEXT PRIMARY KEY,
                    request_hash TEXT NOT NULL CHECK(length(request_hash) = 64),
                    descriptor_json TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def claim_request(self, request_id: str, request_hash: str) -> dict[str, object] | None:
        """Reserve a request identity or return its already registered descriptor."""
        timestamp = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT request_hash, descriptor_json FROM ingestion_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is not None:
                if row[0] != request_hash:
                    raise RegistryError(
                        "REQUEST_ID_CONFLICT",
                        "request_id was already used for different canonical content",
                        False,
                        "validating_request",
                    )
                return json.loads(row[1]) if row[1] is not None else None
            connection.execute(
                "INSERT INTO ingestion_requests("
                "request_id, request_hash, descriptor_json, updated_at"
                ") VALUES(?,?,NULL,?)",
                (request_id, request_hash, timestamp),
            )
        return None

    def complete_request(
        self,
        request_id: str,
        request_hash: str,
        descriptor: DatasetDescriptor,
    ) -> None:
        payload = json.dumps(
            primitive(descriptor), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE ingestion_requests SET descriptor_json=?, updated_at=? "
                "WHERE request_id=? AND request_hash=?",
                (payload, datetime.now(UTC).isoformat(), request_id, request_hash),
            )
            if cursor.rowcount != 1:
                raise RegistryError(
                    "REQUEST_CLAIM_LOST",
                    "request identity could not be completed",
                    True,
                    "registering",
                )

    def next_version(self, dataset_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(version), 0) FROM datasets WHERE dataset_id = ?",
                (dataset_id,),
            ).fetchone()
        return int(row[0]) + 1

    def register(self, descriptor: DatasetDescriptor) -> DatasetRegistration:
        payload = json.dumps(
            primitive(descriptor), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        registered_at = datetime.now(UTC)
        try:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT dataset_hash, descriptor_json FROM datasets "
                    "WHERE dataset_id=? AND version=?",
                    (descriptor.dataset_id, descriptor.version),
                ).fetchone()
                if existing is not None:
                    if existing == (descriptor.dataset_hash, payload):
                        return DatasetRegistration(
                            descriptor=descriptor,
                            registered_at=registered_at,
                            registry_uri=self.path.as_uri(),
                        )
                    raise RegistryError(
                        "REGISTRATION_CONFLICT",
                        "dataset identity already exists with different content",
                        False,
                        "registering",
                    )
                connection.execute(
                    "INSERT INTO datasets("
                    "dataset_id, version, dataset_hash, descriptor_json, "
                    "registered_at"
                    ") VALUES(?,?,?,?,?)",
                    (
                        descriptor.dataset_id,
                        descriptor.version,
                        descriptor.dataset_hash,
                        payload,
                        registered_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise RegistryError(
                "REGISTRATION_CONFLICT",
                "dataset hash or identity already registered",
                False,
                "registering",
            ) from exc
        return DatasetRegistration(
            descriptor=descriptor,
            registered_at=registered_at,
            registry_uri=self.path.as_uri(),
        )

    def register_for_request(
        self,
        descriptor: DatasetDescriptor,
        *,
        request_id: str,
        request_hash: str,
    ) -> DatasetRegistration:
        """Atomically register a dataset and attach it to the claimed request."""
        payload = json.dumps(
            primitive(descriptor), sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        registered_at = datetime.now(UTC)
        try:
            with self._connect() as connection:
                claim = connection.execute(
                    "SELECT request_hash FROM ingestion_requests WHERE request_id=?",
                    (request_id,),
                ).fetchone()
                if claim is None or claim[0] != request_hash:
                    raise RegistryError(
                        "REQUEST_CLAIM_LOST",
                        "request identity is absent or changed",
                        True,
                        "registering",
                    )
                existing = connection.execute(
                    "SELECT dataset_hash, descriptor_json FROM datasets "
                    "WHERE dataset_id=? AND version=?",
                    (descriptor.dataset_id, descriptor.version),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO datasets("
                        "dataset_id, version, dataset_hash, descriptor_json, "
                        "registered_at"
                        ") VALUES(?,?,?,?,?)",
                        (
                            descriptor.dataset_id,
                            descriptor.version,
                            descriptor.dataset_hash,
                            payload,
                            registered_at.isoformat(),
                        ),
                    )
                elif existing != (descriptor.dataset_hash, payload):
                    raise RegistryError(
                        "REGISTRATION_CONFLICT",
                        "dataset identity already exists with different content",
                        False,
                        "registering",
                    )
                connection.execute(
                    "UPDATE ingestion_requests SET descriptor_json=?, updated_at=? "
                    "WHERE request_id=? AND request_hash=?",
                    (payload, registered_at.isoformat(), request_id, request_hash),
                )
        except sqlite3.IntegrityError as exc:
            raise RegistryError(
                "REGISTRATION_CONFLICT",
                "dataset hash or identity already registered",
                False,
                "registering",
            ) from exc
        return DatasetRegistration(
            descriptor=descriptor,
            registered_at=registered_at,
            registry_uri=self.path.as_uri(),
        )

    def inspect(self, dataset_id: str, version: int | None = None) -> dict[str, object] | None:
        query = (
            "SELECT descriptor_json FROM datasets WHERE dataset_id=? AND version=?"
            if version is not None
            else (
                "SELECT descriptor_json FROM datasets WHERE dataset_id=? "
                "ORDER BY version DESC LIMIT 1"
            )
        )
        params = (dataset_id, version) if version is not None else (dataset_id,)
        with self._connect() as connection:
            row = connection.execute(query, params).fetchone()
        return json.loads(row[0]) if row is not None else None

    def health(self) -> dict[str, object]:
        try:
            with self._connect() as connection:
                count = int(connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0])
            return {"status": "ok", "datasets": count, "path": str(self.path)}
        except sqlite3.Error as exc:
            return {"status": "error", "error": type(exc).__name__, "path": str(self.path)}


_DESCRIPTOR_KEYS = {
    "dataset_id",
    "version",
    "dataset_hash",
    "manifest_uri",
    "storage_uri",
    "exchange_id",
    "market_type",
    "instrument_id",
    "data_types",
    "timeframes",
    "start",
    "end",
    "schema_version",
    "validation_status",
    "unresolved_issues",
    "lineage",
    "compatible_consumers",
    "descriptor_version",
}


def _descriptor_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    return value


def _descriptor_integer(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise TypeError(f"{name} must be an integer")
    return value


def _descriptor_strings(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise TypeError(f"{name} must be an array")
    if any(not isinstance(item, str) for item in value):
        raise TypeError(f"{name} must contain only strings")
    return tuple(value)


def _descriptor_datetime(value: object, name: str) -> datetime:
    text = _descriptor_string(value, name)
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 datetime") from exc


def descriptor_from_dict(payload: dict[str, object]) -> DatasetDescriptor:
    from .enums import DataType, MarketType, ValidationStatus

    try:
        if not isinstance(payload, dict):
            raise TypeError("descriptor must be an object")
        keys = set(payload)
        if keys != _DESCRIPTOR_KEYS:
            missing = sorted(_DESCRIPTOR_KEYS - keys)
            unknown = sorted(keys - _DESCRIPTOR_KEYS)
            raise ValueError(f"descriptor fields mismatch; missing={missing}, unknown={unknown}")
        return DatasetDescriptor(
            dataset_id=_descriptor_string(payload["dataset_id"], "dataset_id"),
            version=_descriptor_integer(payload["version"], "version"),
            dataset_hash=_descriptor_string(payload["dataset_hash"], "dataset_hash"),
            manifest_uri=_descriptor_string(payload["manifest_uri"], "manifest_uri"),
            storage_uri=_descriptor_string(payload["storage_uri"], "storage_uri"),
            exchange_id=_descriptor_string(payload["exchange_id"], "exchange_id"),
            market_type=MarketType(_descriptor_string(payload["market_type"], "market_type")),
            instrument_id=_descriptor_string(payload["instrument_id"], "instrument_id"),
            data_types=tuple(
                DataType(item) for item in _descriptor_strings(payload["data_types"], "data_types")
            ),
            timeframes=_descriptor_strings(payload["timeframes"], "timeframes"),
            start=_descriptor_datetime(payload["start"], "start"),
            end=_descriptor_datetime(payload["end"], "end"),
            schema_version=_descriptor_integer(payload["schema_version"], "schema_version"),
            validation_status=ValidationStatus(
                _descriptor_string(payload["validation_status"], "validation_status")
            ),
            unresolved_issues=_descriptor_strings(
                payload["unresolved_issues"], "unresolved_issues"
            ),
            lineage=_descriptor_strings(payload["lineage"], "lineage"),
            compatible_consumers=_descriptor_strings(
                payload["compatible_consumers"], "compatible_consumers"
            ),
            descriptor_version=_descriptor_integer(
                payload["descriptor_version"], "descriptor_version"
            ),
        )
    except RegistryError:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise RegistryError(
            "DESCRIPTOR_SCHEMA_INVALID",
            f"dataset descriptor failed strict validation: {exc}",
            False,
            "reading_registry",
        ) from exc
