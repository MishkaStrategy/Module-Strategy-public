"""Typed contract shared by the SQLite registry implementation mixins."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from threading import Thread, current_thread
from typing import Any, Iterator, Sequence

from .errors import RegistryError
from .models import ExperimentRecord, RegistrationPackage
from .storage_common import RegisterOutcome, _ConflictDetails
from .storage_registration_groups import _RegistrationAccumulator


class StorageMixinBase:
    """Declare the cross-mixin surface used by the concrete SQLite registry.

    The concrete implementation is assembled through multiple inheritance in
    :class:`SQLiteRegistry`. Keeping this contract separate gives static
    analyzers the same surface that exists at runtime without duplicating any
    implementation.
    """

    path: Path
    _raw_connection: sqlite3.Connection
    _owner_thread: Thread
    _read_deadline_active: bool

    def _assert_owner_thread(self) -> None:
        if current_thread() is not self._owner_thread:
            raise RegistryError(
                "SQLiteRegistry instances are single-thread owned; create one "
                "registry instance per worker thread"
            )

    @property
    def _connection(self) -> sqlite3.Connection:
        self._assert_owner_thread()
        return self._raw_connection

    @_connection.setter
    def _connection(self, value: sqlite3.Connection) -> None:
        self._owner_thread = current_thread()
        self._raw_connection = value

    def _initialize(self) -> None:
        raise NotImplementedError

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        raise NotImplementedError
        yield self._connection

    @staticmethod
    def _execute_schema_script(connection: sqlite3.Connection, script: str) -> None:
        raise NotImplementedError

    def _preflight_existing_schema(self) -> int | None:
        raise NotImplementedError

    @staticmethod
    def _record_migration(
        connection: sqlite3.Connection,
        source_version: int,
        target_version: int,
        migration_id: str,
    ) -> None:
        raise NotImplementedError

    def schema_version(self) -> int:
        raise NotImplementedError

    def migration_history(self) -> tuple[dict[str, Any], ...]:
        raise NotImplementedError

    def revision(self) -> int:
        raise NotImplementedError

    @staticmethod
    def _next_revision(connection: sqlite3.Connection) -> int:
        raise NotImplementedError

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
        raise NotImplementedError

    @staticmethod
    def _existing_hash(
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        entity_id: str,
    ) -> str | None:
        raise NotImplementedError

    def _insert_immutable(
        self,
        connection: sqlite3.Connection,
        *,
        table: str,
        id_column: str,
        entity_type: str,
        entity_id: str,
        content_hash: str,
        payload: bytes,
        columns: Sequence[str],
        values: Sequence[Any],
        correlation_id: str,
    ) -> bool:
        raise NotImplementedError

    def _validate_references(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
    ) -> None:
        raise NotImplementedError

    @staticmethod
    def _topological_experiments(
        experiments: Sequence[ExperimentRecord],
    ) -> tuple[ExperimentRecord, ...]:
        raise NotImplementedError

    def _register_primary_entities(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        raise NotImplementedError

    def _register_runs(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        raise NotImplementedError

    def _register_metrics(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        raise NotImplementedError

    def _register_evidence_and_roles(
        self,
        connection: sqlite3.Connection,
        package: RegistrationPackage,
        revision: int,
        accumulator: _RegistrationAccumulator,
    ) -> None:
        raise NotImplementedError

    def register_package(self, package: RegistrationPackage) -> RegisterOutcome:
        raise NotImplementedError

    @staticmethod
    def _decode_stored_payload(
        entity_type: str, payload_raw: object, stored_hash: object
    ) -> dict[str, Any]:
        raise NotImplementedError

    def list_payloads(
        self,
        entity_type: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        raise NotImplementedError

    def quarantine_count(self) -> int:
        raise NotImplementedError

    def quarantine_entries(self) -> tuple[dict[str, Any], ...]:
        raise NotImplementedError

    def _conflict_existing_hash(self, entity_type: str, entity_id: str) -> str | None:
        raise NotImplementedError

    def entity_state_hash(self) -> str:
        raise NotImplementedError

    def verify_integrity(self) -> dict[str, Any]:
        raise NotImplementedError

    def _append_only_triggers_valid(self) -> bool:
        raise NotImplementedError

    def _audit_bindings_valid(self, audit_rows: Sequence[sqlite3.Row]) -> bool:
        raise NotImplementedError

    def _entity_records_valid(self) -> bool:
        raise NotImplementedError

    def _relation_records_valid(self) -> bool:
        raise NotImplementedError

    def _annotation_actor_scope_constraint_valid(self) -> bool:
        raise NotImplementedError

    def _annotation_records_valid(self, current_revision: int) -> bool:
        raise NotImplementedError

    def _migration_records_valid(self) -> bool:
        raise NotImplementedError

    def _commit_quarantine(self, details: _ConflictDetails) -> None:
        raise NotImplementedError
