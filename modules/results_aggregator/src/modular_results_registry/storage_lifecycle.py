from __future__ import annotations

import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from threading import get_ident
from typing import Any, Iterator, Self

from .canonical import canonical_sha256, deterministic_id
from .errors import IntegrityError, RegistryError, ResourceLimitError
from .runtime_capabilities import require_posix_storage_capabilities
from .safe_io import ensure_safe_parent
from .storage_common import (
    _SCHEMA_VERSION,
    _cleanup_created_registry,
    _open_registry_guard,
    _same_registry_entry,
    _validate_sqlite_sidecars,
)
from .storage_mixin_base import StorageMixinBase
from .storage_scalars import strict_schema_version_text, strict_sqlite_integer


class StorageLifecycleMixin(StorageMixinBase):
    def __init__(self, path: str | Path, *, timeout_seconds: float = 30.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        require_posix_storage_capabilities()
        self._owner_thread_id = get_ident()
        self.path = ensure_safe_parent(path)
        _validate_sqlite_sidecars(self.path)
        parent_fd, guard_fd, guard_info, created = _open_registry_guard(self.path)
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(
                self.path,
                isolation_level=None,
                timeout=timeout_seconds,
            )
            if not _same_registry_entry(parent_fd, self.path, guard_info):
                raise IntegrityError("registry path changed during SQLite open")
            self._connection = connection
            self._read_deadline_active = False
            self._connection.row_factory = sqlite3.Row
            _validate_sqlite_sidecars(self.path)
            self._connection.execute("PRAGMA trusted_schema = OFF")
            self._connection.execute("PRAGMA cell_size_check = ON")
            self._connection.execute("PRAGMA foreign_keys = ON")
            self._connection.execute("PRAGMA journal_mode = WAL")
            self._connection.execute("PRAGMA synchronous = FULL")
            busy_timeout_ms = int(timeout_seconds * 1000)
            self._connection.execute("PRAGMA busy_timeout = " + str(busy_timeout_ms))
            self._initialize()
        except BaseException:
            if connection is not None:
                connection.close()
            _cleanup_created_registry(parent_fd, self.path, guard_info, created)
            raise
        finally:
            os.close(guard_fd)
            os.close(parent_fd)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self._connection.close()

    @contextmanager
    def read_deadline(self, timeout_seconds: float) -> Iterator[None]:
        if timeout_seconds <= 0:
            raise ValueError("query timeout must be positive")
        if self._read_deadline_active:
            raise RegistryError("nested query deadlines are forbidden")
        started = time.monotonic()

        def deadline_reached() -> int:
            return int(time.monotonic() - started >= timeout_seconds)

        self._read_deadline_active = True
        self._connection.set_progress_handler(deadline_reached, 1000)
        try:
            yield
        except sqlite3.OperationalError as exc:
            if "interrupted" in str(exc).lower():
                raise ResourceLimitError("query exceeded its execution timeout") from exc
            raise
        finally:
            self._connection.set_progress_handler(None, 0)
            self._read_deadline_active = False

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        if self._connection.in_transaction:
            raise RegistryError("nested registry transactions are forbidden")
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            yield self._connection
            if self._connection.in_transaction:
                self._connection.execute("COMMIT")
        except BaseException as exc:
            if self._connection.in_transaction:
                self._connection.execute("ROLLBACK")
            if (
                isinstance(exc, sqlite3.Error)
                and getattr(exc, "sqlite_errorcode", None) == sqlite3.SQLITE_FULL
            ):
                raise ResourceLimitError("registry storage capacity exhausted") from None
            raise

    @staticmethod
    def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
        statement = ""
        for line in script.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                statement = ""
                if sql:
                    connection.execute(sql)
        if statement.strip():
            raise RegistryError("incomplete registry schema SQL")

    def _preflight_existing_schema(self) -> int | None:
        meta_exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_meta'"
        ).fetchone()
        if meta_exists is None:
            return None
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise RegistryError("registry schema metadata is incomplete")
        version = strict_schema_version_text(row[0])
        if version not in {1, 2, 3, _SCHEMA_VERSION}:
            raise RegistryError(
                f"unsupported registry schema version {row[0]!r}; expected {_SCHEMA_VERSION}"
            )
        if version in {1, 2}:
            metrics_exists = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='metrics'"
            ).fetchone()
            if metrics_exists is not None:
                try:
                    metric_row = self._connection.execute("SELECT COUNT(*) FROM metrics").fetchone()
                except sqlite3.DatabaseError as exc:
                    raise RegistryError("legacy metrics table cannot be inspected") from exc
                if metric_row is None:
                    raise RegistryError("legacy metrics table count is missing")
                metric_count = strict_sqlite_integer(
                    metric_row[0], "legacy metrics count", minimum=0
                )
                if metric_count:
                    raise RegistryError(
                        "registry schema v1/v2 contains metrics without immutable metric "
                        "definitions; automatic inference is forbidden"
                    )
        return version

    @staticmethod
    def _record_migration(
        connection: sqlite3.Connection,
        source_version: int,
        target_version: int,
        migration_id: str,
    ) -> None:
        payload = {
            "migration_id": migration_id,
            "source_version": source_version,
            "target_version": target_version,
        }
        try:
            connection.execute(
                """
                INSERT INTO schema_migrations(
                    migration_id, source_version, target_version, applied_at_utc, report_hash
                ) VALUES(?, ?, ?, ?, ?)
                """,
                (
                    migration_id,
                    source_version,
                    target_version,
                    datetime.now(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z"),
                    canonical_sha256(payload),
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise RegistryError("schema migration history collision") from exc

    def schema_version(self) -> int:
        row = self._connection.execute(
            "SELECT value FROM schema_meta WHERE key='schema_version'"
        ).fetchone()
        if row is None:
            raise IntegrityError("schema version metadata is missing")
        return strict_schema_version_text(row[0])

    def migration_history(self) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            """
            SELECT migration_id, source_version, target_version, applied_at_utc, report_hash
            FROM schema_migrations ORDER BY rowid
            """
        ).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            item["source_version"] = strict_sqlite_integer(
                item.get("source_version"), "migration source_version", minimum=0
            )
            item["target_version"] = strict_sqlite_integer(
                item.get("target_version"), "migration target_version", minimum=1
            )
            result.append(item)
        return tuple(result)

    def revision(self) -> int:
        row = self._connection.execute(
            "SELECT revision FROM registry_state WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise IntegrityError("registry_state singleton is missing")
        return strict_sqlite_integer(row[0], "registry revision", minimum=0)

    @staticmethod
    def _next_revision(connection: sqlite3.Connection) -> int:
        connection.execute("UPDATE registry_state SET revision=revision+1 WHERE singleton=1")
        row = connection.execute("SELECT revision FROM registry_state WHERE singleton=1").fetchone()
        if row is None:
            raise IntegrityError("registry_state singleton is missing")
        return strict_sqlite_integer(row[0], "registry revision", minimum=1)

    @staticmethod
    def _audit(
        connection: sqlite3.Connection,
        *,
        revision: int,
        operation: str,
        entity_type: str,
        entity_id: str,
        previous_state: str | None,
        new_state: str,
        reason_code: str,
        correlation_id: str,
        payload_hash: str,
    ) -> None:
        event_payload = {
            "revision": revision,
            "operation": operation,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "previous_state": previous_state,
            "new_state": new_state,
            "reason_code": reason_code,
            "correlation_id": correlation_id,
            "payload_hash": payload_hash,
        }
        event_id = deterministic_id("evt", event_payload, digest_chars=48)
        timestamp = datetime.now(timezone.utc)
        previous_row = connection.execute(
            "SELECT timestamp_utc FROM audit_log ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        if previous_row is not None:
            previous_timestamp = datetime.fromisoformat(
                str(previous_row[0]).replace("Z", "+00:00")
            ).astimezone(timezone.utc)
            if timestamp < previous_timestamp:
                timestamp = previous_timestamp
        timestamp_utc = timestamp.isoformat(timespec="microseconds").replace("+00:00", "Z")
        connection.execute(
            """
            INSERT INTO audit_log(
                event_id, timestamp_utc, actor, operation, entity_type, entity_id,
                previous_state, new_state, reason_code, correlation_id,
                payload_hash, revision
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                timestamp_utc,
                "modular-results-registry",
                operation,
                entity_type,
                entity_id,
                previous_state,
                new_state,
                reason_code,
                correlation_id,
                payload_hash,
                revision,
            ),
        )
