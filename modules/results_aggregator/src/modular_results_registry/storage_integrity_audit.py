from __future__ import annotations

import sqlite3
from typing import Sequence

from .errors import RegistryError
from .storage_common import (
    _APPEND_ONLY_TRIGGER_SPECS,
    _ENTITY_QUERIES,
)

from .storage_mixin_base import StorageMixinBase


class StorageIntegrityAuditMixin(StorageMixinBase):
    def _append_only_triggers_valid(self) -> bool:
        rows = self._connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
        ).fetchall()
        definitions = {str(row[0]): str(row[1] or "") for row in rows}
        for name, (operation, table) in _APPEND_ONLY_TRIGGER_SPECS.items():
            sql = " ".join(definitions.get(name, "").lower().split())
            if not sql:
                return False
            required = (
                f"before {operation.lower()} on {table}",
                "raise(abort",
                f"{table} is append-only",
            )
            if any(token not in sql for token in required):
                return False
        return True

    def _audit_bindings_valid(self, audit_rows: Sequence[sqlite3.Row]) -> bool:
        try:
            for row in audit_rows:
                operation = str(row["operation"])
                entity_type = str(row["entity_type"])
                entity_id = str(row["entity_id"])
                payload_hash = str(row["payload_hash"])
                revision = int(row["revision"])
                correlation_id = str(row["correlation_id"])
                if operation == "registered":
                    if entity_type not in _ENTITY_QUERIES or entity_type in {
                        "package",
                        "annotation",
                    }:
                        return False
                    table, id_column = _ENTITY_QUERIES[entity_type]
                    if (
                        self._existing_hash(
                            self._connection,
                            table=table,
                            id_column=id_column,
                            entity_id=entity_id,
                        )
                        != payload_hash
                    ):
                        return False
                elif operation == "package_registered":
                    if entity_type != "package":
                        return False
                    if (
                        self._existing_hash(
                            self._connection,
                            table="packages",
                            id_column="package_id",
                            entity_id=entity_id,
                        )
                        != payload_hash
                    ):
                        return False
                elif operation == "quarantined":
                    match = self._connection.execute(
                        """
                            SELECT 1 FROM quarantine
                            WHERE entity_type=? AND entity_id=?
                              AND incoming_content_hash=? AND correlation_id=?
                              AND revision=?
                            """,
                        (entity_type, entity_id, payload_hash, correlation_id, revision),
                    ).fetchone()
                    if match is None:
                        return False
                elif operation == "ANNOTATED":
                    match = self._connection.execute(
                        """
                            SELECT 1 FROM registry_annotations
                            WHERE annotation_id=? AND entity_type=? AND entity_id=?
                              AND reason=? AND payload_hash=? AND new_revision=?
                            """,
                        (
                            correlation_id,
                            entity_type,
                            entity_id,
                            str(row["reason_code"]),
                            payload_hash,
                            revision,
                        ),
                    ).fetchone()
                    if match is None:
                        return False
                else:
                    return False
            return True
        except (RegistryError, TypeError, ValueError, sqlite3.DatabaseError):
            return False
