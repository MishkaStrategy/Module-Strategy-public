from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .canonical import canonical_sha256, deterministic_id
from .errors import IntegrityError, RegistryError
from .models import (
    validate_sha256,
)
from .storage_common import (
    _REGISTERED_REVISION_TABLES,
    _sql_identifier,
)

from .storage_mixin_base import StorageMixinBase


class StorageIntegrityMixin(StorageMixinBase):
    def verify_integrity(self) -> dict[str, Any]:
        integrity_row = self._connection.execute("PRAGMA integrity_check").fetchone()
        foreign_rows = self._connection.execute("PRAGMA foreign_key_check").fetchall()
        audit_rows = self._connection.execute(
            """
                SELECT sequence, event_id, timestamp_utc, actor, operation, entity_type,
                       entity_id, previous_state, new_state, reason_code, correlation_id,
                       payload_hash, revision
                FROM audit_log ORDER BY sequence
                """
        ).fetchall()
        sequences = [int(row["sequence"]) for row in audit_rows]
        revisions = [int(row["revision"]) for row in audit_rows]
        sequence_contiguous = sequences == list(range(1, len(sequences) + 1))
        revision_monotonic = all(left <= right for left, right in zip(revisions, revisions[1:]))
        event_ids_valid = True
        timestamps_valid = True
        timestamp_values: list[datetime] = []
        chain_hash = "0" * 64
        for row in audit_rows:
            event_payload = {
                "revision": int(row["revision"]),
                "operation": str(row["operation"]),
                "entity_type": str(row["entity_type"]),
                "entity_id": str(row["entity_id"]),
                "previous_state": row["previous_state"],
                "new_state": str(row["new_state"]),
                "reason_code": str(row["reason_code"]),
                "correlation_id": str(row["correlation_id"]),
                "payload_hash": str(row["payload_hash"]),
            }
            expected_event_id = deterministic_id("evt", event_payload, digest_chars=48)
            event_ids_valid = event_ids_valid and str(row["event_id"]) == expected_event_id
            try:
                timestamp = datetime.fromisoformat(str(row["timestamp_utc"]).replace("Z", "+00:00"))
                if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                    raise ValueError
                timestamp_values.append(timestamp.astimezone(timezone.utc))
            except ValueError:
                timestamps_valid = False
            chain_hash = canonical_sha256(
                {
                    "previous_hash": chain_hash,
                    "sequence": int(row["sequence"]),
                    "event_id": str(row["event_id"]),
                    "timestamp_utc": str(row["timestamp_utc"]),
                    "actor": str(row["actor"]),
                    **event_payload,
                }
            )
        timestamp_monotonic = timestamps_valid and all(
            left <= right for left, right in zip(timestamp_values, timestamp_values[1:])
        )
        audit_bindings_valid = self._audit_bindings_valid(audit_rows)
        max_audit_revision = max(revisions, default=0)
        current_revision = self.revision()
        entity_records_valid = self._entity_records_valid()
        relation_records_valid = self._relation_records_valid()
        annotation_records_valid = self._annotation_records_valid(current_revision)
        annotation_actor_scope_constraint = self._annotation_actor_scope_constraint_valid()
        migration_records_valid = self._migration_records_valid()
        quarantine_records_valid = True
        try:
            quarantine_entries = self.quarantine_entries()
        except RegistryError:
            quarantine_entries = ()
            quarantine_records_valid = False
        for entry in quarantine_entries:
            expected_quarantine_id = deterministic_id(
                "quarantine",
                {
                    "entity_type": entry["entity_type"],
                    "entity_id": entry["entity_id"],
                    "incoming_content_hash": entry["incoming_content_hash"],
                },
                digest_chars=48,
            )
            try:
                validate_sha256("existing_content_hash", str(entry["existing_content_hash"]))
                validate_sha256("incoming_content_hash", str(entry["incoming_content_hash"]))
                validate_sha256("payload_bytes_sha256", str(entry["payload_bytes_sha256"]))
                timestamp = datetime.fromisoformat(
                    str(entry["created_at_utc"]).replace("Z", "+00:00")
                )
                canonical_timestamp = (
                    timestamp.astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                current_existing_hash = self._conflict_existing_hash(
                    str(entry["entity_type"]), str(entry["entity_id"])
                )
            except (RegistryError, ValueError):
                quarantine_records_valid = False
                continue
            quarantine_records_valid = quarantine_records_valid and all(
                (
                    str(entry["quarantine_id"]) == expected_quarantine_id,
                    str(entry["reason_code"]) == "IDENTIFIER_CONTENT_CONFLICT",
                    bool(str(entry["correlation_id"])),
                    canonical_timestamp == str(entry["created_at_utc"]),
                    current_existing_hash == str(entry["existing_content_hash"]),
                    int(entry["revision"]) <= current_revision,
                )
            )

        append_only_triggers_present = self._append_only_triggers_valid()

        registered_revision_rows: list[int] = []
        for table in _REGISTERED_REVISION_TABLES:
            rows = self._connection.execute(
                "SELECT registered_revision FROM " + _sql_identifier(table)
            ).fetchall()
            registered_revision_rows.extend(int(row[0]) for row in rows)
        registered_revisions_valid = all(
            1 <= revision <= current_revision for revision in registered_revision_rows
        )

        operation_revision_rows = self._connection.execute(
            """
                SELECT registered_revision AS revision FROM packages
                UNION ALL
                SELECT new_revision AS revision FROM registry_annotations
                UNION ALL
                SELECT revision FROM quarantine
                ORDER BY revision
                """
        ).fetchall()
        operation_revisions = [int(row[0]) for row in operation_revision_rows]
        operation_revision_coverage = operation_revisions == list(range(1, current_revision + 1))
        audit_revision_coverage = sorted(set(revisions)) == list(range(1, current_revision + 1))
        try:
            entity_state_hash: str | None = self.entity_state_hash()
        except RegistryError:
            entity_state_hash = None
        result = {
            "schema_version": self.schema_version(),
            "migration_count": len(self.migration_history()),
            "sqlite_integrity": "missing" if integrity_row is None else str(integrity_row[0]),
            "foreign_key_violations": len(foreign_rows),
            "audit_sequence_contiguous": sequence_contiguous,
            "audit_revision_monotonic": revision_monotonic,
            "audit_revision_not_ahead": max_audit_revision <= self.revision(),
            "audit_event_ids_valid": event_ids_valid,
            "audit_bindings_valid": audit_bindings_valid,
            "audit_timestamps_valid": timestamps_valid,
            "audit_timestamp_monotonic": timestamp_monotonic,
            "audit_revision_coverage": audit_revision_coverage,
            "audit_event_count": len(audit_rows),
            "audit_chain_hash": chain_hash,
            "operation_revision_coverage": operation_revision_coverage,
            "quarantine_records_valid": quarantine_records_valid,
            "entity_records_valid": entity_records_valid,
            "relation_records_valid": relation_records_valid,
            "annotation_records_valid": annotation_records_valid,
            "annotation_actor_scope_constraint": annotation_actor_scope_constraint,
            "migration_records_valid": migration_records_valid,
            "append_only_triggers_present": append_only_triggers_present,
            "registered_revisions_valid": registered_revisions_valid,
            "registry_revision": current_revision,
            "quarantine_count": self.quarantine_count(),
            "entity_state_hash": entity_state_hash,
        }
        result["integrity_hash"] = canonical_sha256(result)
        return result

    def checkpoint_wal(self) -> None:
        row = self._connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if row is None or int(row[0]) not in {0, 1}:
            raise IntegrityError("WAL checkpoint failed")
