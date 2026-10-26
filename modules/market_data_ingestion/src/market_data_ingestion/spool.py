from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from collections import defaultdict, deque
from collections.abc import Iterable, Iterator
from pathlib import Path
from types import TracebackType
from typing import Final

from .canonical import primitive
from .enums import DataType
from .errors import StorageError
from .records import NormalizedRecord, normalized_record_from_row

_SCHEMA_VERSION: Final[int] = 2
_DEFAULT_BATCH_SIZE: Final[int] = 8_192
_PENDING_ROWID_LIMIT: Final[int] = 1_024
_ALLOWED_STAGES: Final[frozenset[str]] = frozenset({"acquired", "validated"})
_EXPECTED_RECORD_COLUMNS: Final[tuple[str, ...]] = (
    "stage",
    "data_type",
    "timeframe",
    "timestamp",
    "end_time",
    "identity",
    "payload",
)
_EXPECTED_VALIDATED_COLUMNS: Final[tuple[str, ...]] = ("record_rowid",)


def _stage(value: str) -> str:
    if value not in _ALLOWED_STAGES:
        raise ValueError(f"unsupported record spool stage: {value}")
    return value


def _canonical_payload(row: dict[str, object]) -> str:
    return json.dumps(
        row,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )


class RecordSpool:
    """Bounded-memory, deterministic normalized-record staging database.

    Acquired rows own the canonical serialized payload. Validated rows normally
    reference the already verified acquired row through ``validated_records``.
    This avoids writing and later decoding a second identical JSON payload while
    preserving deterministic ordering, duplicate evidence, and tamper checks.
    """

    def __init__(
        self,
        path: Path,
        *,
        reset: bool = False,
        batch_size: int = _DEFAULT_BATCH_SIZE,
    ) -> None:
        if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
            raise ValueError("batch_size must be a positive integer")
        self.path = path
        self._batch_size = batch_size
        self._pending_rowids: dict[NormalizedRecord, deque[int]] = defaultdict(deque)
        self._pending_rowid_order: deque[tuple[NormalizedRecord, int]] = deque()
        self._pending_rowid_limit = _PENDING_ROWID_LIMIT
        self._validation_link_buffer: list[tuple[int]] = []
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
        if self.path.is_symlink():
            raise StorageError(
                "SPOOL_PATH_UNSAFE",
                "record spool path must not be a symbolic link",
                False,
                "staging",
            )
        if self.path.exists() and not self.path.is_file():
            raise StorageError(
                "SPOOL_PATH_UNSAFE",
                "record spool path must be a regular file",
                False,
                "staging",
            )
        if reset:
            self.path.unlink(missing_ok=True)
        self._closed = True
        try:
            self._connection = sqlite3.connect(self.path)
            os.chmod(self.path, 0o600)
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=FULL")
            self._connection.execute("PRAGMA temp_store=FILE")
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS spool_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS records (
                    stage TEXT NOT NULL,
                    data_type TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    end_time TEXT NOT NULL,
                    identity TEXT NOT NULL,
                    payload TEXT NOT NULL
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS validated_records (
                    record_rowid INTEGER PRIMARY KEY
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_stream
                ON records(stage, data_type, timeframe, timestamp, identity)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_records_validation_order
                ON records(data_type, timeframe, timestamp, identity)
                """
            )
            existing = self._connection.execute(
                "SELECT value FROM spool_metadata WHERE key = 'schema_version'"
            ).fetchone()
            if existing is None:
                self._connection.execute(
                    "INSERT INTO spool_metadata(key, value) VALUES('schema_version', ?)",
                    (str(_SCHEMA_VERSION),),
                )
            elif existing[0] != str(_SCHEMA_VERSION):
                raise StorageError(
                    "SPOOL_SCHEMA_UNSUPPORTED",
                    "record spool schema version is not supported",
                    False,
                    "staging",
                )
            self._connection.commit()
            self._verify_schema()
            self._closed = False
        except StorageError:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise
        except sqlite3.Error as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise StorageError(
                "SPOOL_OPEN_FAILED",
                "record spool database could not be opened or initialized",
                False,
                "staging",
                {"error_type": type(exc).__name__},
            ) from exc
        except OSError as exc:
            connection = getattr(self, "_connection", None)
            if connection is not None:
                connection.close()
            raise StorageError(
                "SPOOL_FILESYSTEM_FAILED",
                "record spool filesystem initialization failed",
                False,
                "staging",
                {"error_type": type(exc).__name__},
            ) from exc

    @property
    def batch_size(self) -> int:
        return self._batch_size

    @property
    def pending_rowid_limit(self) -> int:
        return self._pending_rowid_limit

    def _remember_pending_rowid(
        self,
        record: NormalizedRecord,
        rowid: int,
    ) -> None:
        self._pending_rowids[record].append(rowid)
        self._pending_rowid_order.append((record, rowid))
        while len(self._pending_rowid_order) > self._pending_rowid_limit:
            oldest_record, oldest_rowid = self._pending_rowid_order.popleft()
            pending = self._pending_rowids.get(oldest_record)
            if not pending or pending[0] != oldest_rowid:
                continue
            pending.popleft()
            if not pending:
                del self._pending_rowids[oldest_record]

    def _verify_schema(self) -> None:
        try:
            record_object = self._connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'records'"
            ).fetchone()
            metadata_object = self._connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'spool_metadata'"
            ).fetchone()
            validated_object = self._connection.execute(
                "SELECT type FROM sqlite_master WHERE name = 'validated_records'"
            ).fetchone()
            if (
                record_object != ("table",)
                or metadata_object != ("table",)
                or validated_object != ("table",)
            ):
                raise StorageError(
                    "SPOOL_SCHEMA_INVALID",
                    "record spool requires physical metadata, records, and validation tables",
                    False,
                    "staging",
                )
            columns = tuple(
                str(row[1]) for row in self._connection.execute("PRAGMA table_info(records)")
            )
            if columns != _EXPECTED_RECORD_COLUMNS:
                raise StorageError(
                    "SPOOL_SCHEMA_INVALID",
                    "record spool columns do not match the supported schema",
                    False,
                    "staging",
                    {
                        "expected": list(_EXPECTED_RECORD_COLUMNS),
                        "observed": list(columns),
                    },
                )
            metadata_columns = tuple(
                str(row[1]) for row in self._connection.execute("PRAGMA table_info(spool_metadata)")
            )
            if metadata_columns != ("key", "value"):
                raise StorageError(
                    "SPOOL_SCHEMA_INVALID",
                    "record spool metadata columns do not match the supported schema",
                    False,
                    "staging",
                )
            validated_columns = tuple(
                str(row[1])
                for row in self._connection.execute("PRAGMA table_info(validated_records)")
            )
            if validated_columns != _EXPECTED_VALIDATED_COLUMNS:
                raise StorageError(
                    "SPOOL_SCHEMA_INVALID",
                    "record spool validation-link columns do not match the supported schema",
                    False,
                    "staging",
                    {
                        "expected": list(_EXPECTED_VALIDATED_COLUMNS),
                        "observed": list(validated_columns),
                    },
                )
        except sqlite3.Error as exc:
            raise StorageError(
                "SPOOL_SCHEMA_INVALID",
                "record spool schema could not be inspected",
                False,
                "staging",
                {"error_type": type(exc).__name__},
            ) from exc

    @staticmethod
    def _serialized_record(
        stage: str,
        record: NormalizedRecord,
    ) -> tuple[str, str, str, str, str, str, str]:
        row = primitive(record.to_row())
        if not isinstance(row, dict):
            raise TypeError("normalized record row must serialize to a mapping")
        payload = _canonical_payload(row)
        identity = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        return (
            stage,
            record.data_type.value,
            record.record_timeframe or "",
            record.timestamp.isoformat(),
            record.end_time.isoformat(),
            identity,
            payload,
        )

    def append(self, stage: str, records: Iterable[NormalizedRecord]) -> int:
        normalized_stage = _stage(stage)
        record_batch: list[tuple[str, str, str, str, str, str, str]] = []
        count = 0
        for record in records:
            if normalized_stage == "validated":
                pending = self._pending_rowids.get(record)
                if pending:
                    self._validation_link_buffer.append((pending.popleft(),))
                    if not pending:
                        del self._pending_rowids[record]
                    if len(self._validation_link_buffer) >= self._batch_size:
                        self._flush_validation_links()
                else:
                    record_batch.append(self._serialized_record(normalized_stage, record))
            else:
                record_batch.append(self._serialized_record(normalized_stage, record))
            count += 1
            if len(record_batch) >= self._batch_size:
                self._insert_records(record_batch)
                record_batch.clear()
        if record_batch:
            self._insert_records(record_batch)
        return count

    def _insert_records(
        self,
        records: list[tuple[str, str, str, str, str, str, str]],
    ) -> None:
        try:
            with self._connection:
                self._connection.executemany(
                    """
                    INSERT INTO records(
                        stage, data_type, timeframe, timestamp,
                        end_time, identity, payload
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    records,
                )
        except sqlite3.Error as exc:
            raise StorageError(
                "SPOOL_WRITE_FAILED",
                "normalized record staging database write failed",
                True,
                "staging",
                {"error_type": type(exc).__name__},
            ) from exc

    def _flush_validation_links(self) -> None:
        if not self._validation_link_buffer:
            return
        links = self._validation_link_buffer
        self._validation_link_buffer = []
        try:
            with self._connection:
                self._connection.executemany(
                    """
                    INSERT INTO validated_records(record_rowid)
                    VALUES (?)
                    """,
                    links,
                )
        except sqlite3.Error as exc:
            self._validation_link_buffer = [*links, *self._validation_link_buffer]
            raise StorageError(
                "SPOOL_WRITE_FAILED",
                "normalized record validation-link write failed",
                True,
                "staging",
                {"error_type": type(exc).__name__},
            ) from exc

    def iter_sorted(
        self,
        stage: str,
        data_type: DataType,
        timeframe: str | None,
    ) -> Iterator[NormalizedRecord]:
        normalized_stage = _stage(stage)
        if normalized_stage == "validated":
            self._flush_validation_links()
        try:
            if normalized_stage == "validated":
                cursor = self._connection.execute(
                    """
                    SELECT
                        records.rowid,
                        records.data_type,
                        records.timeframe,
                        records.timestamp,
                        records.end_time,
                        records.identity,
                        records.payload
                    FROM records
                    WHERE records.data_type = ?
                      AND records.timeframe = ?
                      AND (
                        records.stage = 'validated'
                        OR EXISTS (
                            SELECT 1
                            FROM validated_records
                            WHERE validated_records.record_rowid = records.rowid
                        )
                      )
                    ORDER BY records.timestamp ASC, records.identity ASC, records.rowid ASC
                    """,
                    (data_type.value, timeframe or ""),
                )
            else:
                cursor = self._connection.execute(
                    """
                    SELECT
                        rowid, data_type, timeframe, timestamp,
                        end_time, identity, payload
                    FROM records
                    WHERE stage = ? AND data_type = ? AND timeframe = ?
                    ORDER BY timestamp ASC, identity ASC, rowid ASC
                    """,
                    (normalized_stage, data_type.value, timeframe or ""),
                )
        except sqlite3.Error as exc:
            raise StorageError(
                "SPOOL_READ_FAILED",
                "normalized record staging database read failed",
                False,
                "staging",
                {"error_type": type(exc).__name__},
            ) from exc
        for (
            rowid,
            stored_data_type,
            stored_timeframe,
            stored_timestamp,
            stored_end_time,
            stored_identity,
            payload,
        ) in cursor:
            if not isinstance(payload, str):
                raise StorageError(
                    "SPOOL_PAYLOAD_CORRUPT",
                    "normalized record staging payload is not text",
                    False,
                    "staging",
                )
            try:
                row = json.loads(payload)
            except (TypeError, json.JSONDecodeError) as exc:
                raise StorageError(
                    "SPOOL_PAYLOAD_CORRUPT",
                    "normalized record staging payload is invalid JSON",
                    False,
                    "staging",
                ) from exc
            if not isinstance(row, dict):
                raise StorageError(
                    "SPOOL_PAYLOAD_CORRUPT",
                    "normalized record staging payload is not an object",
                    False,
                    "staging",
                )
            canonical_payload = _canonical_payload(row)
            expected_identity = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()
            try:
                record = normalized_record_from_row(data_type, row)
            except (TypeError, ValueError) as exc:
                raise StorageError(
                    "SPOOL_PAYLOAD_CORRUPT",
                    "normalized record staging payload violates its record schema",
                    False,
                    "staging",
                ) from exc
            normalized_row = primitive(record.to_row())
            if not isinstance(normalized_row, dict):
                raise StorageError(
                    "SPOOL_PAYLOAD_CORRUPT",
                    "normalized record round-trip is not an object",
                    False,
                    "staging",
                )
            metadata_matches = (
                payload == canonical_payload
                and row == normalized_row
                and stored_data_type == record.data_type.value
                and stored_timeframe == (record.record_timeframe or "")
                and stored_timestamp == record.timestamp.isoformat()
                and stored_end_time == record.end_time.isoformat()
                and stored_identity == expected_identity
            )
            if not metadata_matches:
                raise StorageError(
                    "SPOOL_METADATA_CORRUPT",
                    "record spool index metadata differs from the normalized payload",
                    False,
                    "staging",
                    {
                        "stored_data_type": stored_data_type,
                        "stored_timeframe": stored_timeframe,
                        "stored_timestamp": stored_timestamp,
                    },
                )
            if normalized_stage == "acquired":
                self._remember_pending_rowid(record, int(rowid))
            yield record

    def count(self, stage: str, data_type: DataType, timeframe: str | None) -> int:
        normalized_stage = _stage(stage)
        if normalized_stage == "validated":
            self._flush_validation_links()
        if normalized_stage == "validated":
            row = self._connection.execute(
                """
                SELECT COUNT(*)
                FROM records
                WHERE data_type = ?
                  AND timeframe = ?
                  AND (
                    stage = 'validated'
                    OR EXISTS (
                        SELECT 1
                        FROM validated_records
                        WHERE validated_records.record_rowid = records.rowid
                    )
                  )
                """,
                (data_type.value, timeframe or ""),
            ).fetchone()
        else:
            row = self._connection.execute(
                """
                SELECT COUNT(*) FROM records
                WHERE stage = ? AND data_type = ? AND timeframe = ?
                """,
                (normalized_stage, data_type.value, timeframe or ""),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def clear_stage(self, stage: str) -> None:
        normalized_stage = _stage(stage)
        self._flush_validation_links()
        with self._connection:
            if normalized_stage == "validated":
                self._connection.execute("DELETE FROM validated_records")
                self._connection.execute("DELETE FROM records WHERE stage = 'validated'")
            else:
                self._connection.execute(
                    """
                    DELETE FROM validated_records
                    WHERE record_rowid IN (
                        SELECT rowid FROM records WHERE stage = ?
                    )
                    """,
                    (normalized_stage,),
                )
                self._connection.execute("DELETE FROM records WHERE stage = ?", (normalized_stage,))
        self._pending_rowids.clear()
        self._pending_rowid_order.clear()

    def clear_stream(
        self,
        stage: str,
        data_type: DataType,
        timeframe: str | None,
    ) -> None:
        normalized_stage = _stage(stage)
        self._flush_validation_links()
        with self._connection:
            if normalized_stage == "validated":
                self._connection.execute(
                    """
                    DELETE FROM validated_records
                    WHERE record_rowid IN (
                        SELECT rowid
                        FROM records
                        WHERE data_type = ? AND timeframe = ?
                    )
                    """,
                    (data_type.value, timeframe or ""),
                )
                self._connection.execute(
                    """
                    DELETE FROM records
                    WHERE stage = 'validated' AND data_type = ? AND timeframe = ?
                    """,
                    (data_type.value, timeframe or ""),
                )
            else:
                self._connection.execute(
                    """
                    DELETE FROM validated_records
                    WHERE record_rowid IN (
                        SELECT rowid
                        FROM records
                        WHERE stage = ? AND data_type = ? AND timeframe = ?
                    )
                    """,
                    (normalized_stage, data_type.value, timeframe or ""),
                )
                self._connection.execute(
                    """
                    DELETE FROM records
                    WHERE stage = ? AND data_type = ? AND timeframe = ?
                    """,
                    (normalized_stage, data_type.value, timeframe or ""),
                )
        self._pending_rowids.clear()
        self._pending_rowid_order.clear()

    def close(self, *, remove: bool = False) -> None:
        if self._closed:
            return
        self._flush_validation_links()
        self._connection.close()
        self._closed = True
        self._pending_rowids.clear()
        self._pending_rowid_order.clear()
        self._validation_link_buffer.clear()
        if remove:
            self.path.unlink(missing_ok=True)
            Path(f"{self.path}-wal").unlink(missing_ok=True)
            Path(f"{self.path}-shm").unlink(missing_ok=True)

    def __enter__(self) -> RecordSpool:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close(remove=True)
