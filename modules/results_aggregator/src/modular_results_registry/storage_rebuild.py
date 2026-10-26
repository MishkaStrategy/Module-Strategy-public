from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from .canonical import canonical_json_bytes, canonical_sha256, deterministic_id
from .errors import IntegrityError
from .models import (
    validate_sha256,
)
from .storage_common import (
    _ENTITY_QUERIES,
    _ENTITY_STATE_KEYS,
    _SCHEMA_VERSION,
)

from .storage_mixin_base import StorageMixinBase


class StorageRebuildMixin(StorageMixinBase):
    def _conflict_existing_hash(self, entity_type: str, entity_id: str) -> str | None:
        if entity_type == "role":
            experiment_id, separator, configuration_id = entity_id.partition(":")
            if not separator or not experiment_id or not configuration_id:
                raise IntegrityError("invalid role conflict identifier")
            row = self._connection.execute(
                """
                SELECT content_hash FROM experiment_roles
                WHERE experiment_id=? AND configuration_id=?
                """,
                (experiment_id, configuration_id),
            ).fetchone()
            return None if row is None else str(row[0])
        if entity_type not in _ENTITY_QUERIES or entity_type == "annotation":
            raise IntegrityError("unsupported quarantine entity type")
        table, id_column = _ENTITY_QUERIES[entity_type]
        return self._existing_hash(
            self._connection,
            table=table,
            id_column=id_column,
            entity_id=entity_id,
        )

    def _restore_quarantine_entry(self, raw: Mapping[str, Any]) -> int:
        required = {
            "quarantine_id",
            "entity_type",
            "entity_id",
            "existing_content_hash",
            "incoming_content_hash",
            "payload",
            "payload_bytes_sha256",
            "reason_code",
            "correlation_id",
            "revision",
            "created_at_utc",
        }
        if set(raw) != required:
            raise IntegrityError("quarantine rebuild entry fields are invalid")
        entity_type = str(raw["entity_type"])
        entity_id = str(raw["entity_id"])
        existing_hash = str(raw["existing_content_hash"])
        incoming_hash = str(raw["incoming_content_hash"])
        validate_sha256("existing_content_hash", existing_hash)
        validate_sha256("incoming_content_hash", incoming_hash)
        payload_bytes_hash = str(raw["payload_bytes_sha256"])
        validate_sha256("payload_bytes_sha256", payload_bytes_hash)
        payload_bytes = canonical_json_bytes(raw["payload"])
        if hashlib.sha256(payload_bytes).hexdigest() != payload_bytes_hash:
            raise IntegrityError("quarantine payload checksum mismatch")
        if canonical_sha256(raw["payload"]) != incoming_hash:
            raise IntegrityError("quarantine payload does not match incoming_content_hash")
        expected_id = deterministic_id(
            "quarantine",
            {
                "entity_type": entity_type,
                "entity_id": entity_id,
                "incoming_content_hash": incoming_hash,
            },
            digest_chars=48,
        )
        if str(raw["quarantine_id"]) != expected_id:
            raise IntegrityError("quarantine identity mismatch")
        if str(raw["reason_code"]) != "IDENTIFIER_CONTENT_CONFLICT":
            raise IntegrityError("unsupported quarantine reason")
        correlation_id = str(raw["correlation_id"])
        if not correlation_id:
            raise IntegrityError("quarantine correlation_id must be non-empty")
        try:
            revision = int(raw["revision"])
        except (TypeError, ValueError) as exc:
            raise IntegrityError("quarantine revision is invalid") from exc
        if revision != self.revision() + 1:
            raise IntegrityError("quarantine revision is out of sequence")
        created_at_raw = str(raw["created_at_utc"])
        try:
            created_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise IntegrityError("quarantine created_at_utc is invalid") from exc
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise IntegrityError("quarantine created_at_utc must be timezone-aware")
        created_at_utc = (
            created_at.astimezone(timezone.utc)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        if created_at_raw != created_at_utc:
            raise IntegrityError("quarantine created_at_utc is not canonical UTC")
        if self._conflict_existing_hash(entity_type, entity_id) != existing_hash:
            raise IntegrityError("quarantine existing entity hash mismatch")
        with self.transaction() as connection:
            next_revision = self._next_revision(connection)
            if next_revision != revision:
                raise IntegrityError("quarantine revision replay mismatch")
            connection.execute(
                """
                INSERT INTO quarantine(
                    quarantine_id, entity_type, entity_id, existing_content_hash,
                    incoming_content_hash, payload_json, reason_code, correlation_id,
                    revision, created_at_utc
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    expected_id,
                    entity_type,
                    entity_id,
                    existing_hash,
                    incoming_hash,
                    payload_bytes,
                    "IDENTIFIER_CONTENT_CONFLICT",
                    correlation_id,
                    revision,
                    created_at_utc,
                ),
            )
            self._audit(
                connection,
                revision=revision,
                operation="quarantined",
                entity_type=entity_type,
                entity_id=entity_id,
                previous_state="REGISTERED",
                new_state="QUARANTINED",
                reason_code="IDENTIFIER_CONTENT_CONFLICT",
                correlation_id=correlation_id,
                payload_hash=incoming_hash,
            )
        return revision

    def _restore_audit_timestamps(self, expected_events: Sequence[Mapping[str, Any]]) -> None:
        fields = (
            "sequence",
            "event_id",
            "timestamp_utc",
            "actor",
            "operation",
            "entity_type",
            "entity_id",
            "previous_state",
            "new_state",
            "reason_code",
            "correlation_id",
            "payload_hash",
            "revision",
        )
        generated_rows = self._connection.execute(
            "SELECT " + ",".join(fields) + " FROM audit_log ORDER BY sequence"
        ).fetchall()
        if len(generated_rows) != len(expected_events):
            raise IntegrityError("rebuild audit event count mismatch")
        timestamps: list[str] = []
        previous_timestamp: datetime | None = None
        for index, (generated, expected_raw) in enumerate(
            zip(generated_rows, expected_events, strict=True), start=1
        ):
            if set(expected_raw) != set(fields):
                raise IntegrityError("rebuild audit event fields are invalid")
            try:
                expected_sequence = int(expected_raw["sequence"])
                int(expected_raw["revision"])
            except (TypeError, ValueError) as exc:
                raise IntegrityError("rebuild audit numeric field is invalid") from exc
            if expected_sequence != index:
                raise IntegrityError("rebuild audit sequence is not contiguous")
            for field in fields:
                if field == "timestamp_utc":
                    continue
                expected_value: Any = expected_raw[field]
                if field in {"sequence", "revision"}:
                    expected_value = int(expected_value)
                if generated[field] != expected_value:
                    raise IntegrityError(f"rebuild audit semantic mismatch: {field}")
            timestamp_raw = str(expected_raw["timestamp_utc"])
            try:
                timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
            except ValueError as exc:
                raise IntegrityError("rebuild audit timestamp is invalid") from exc
            if timestamp.tzinfo is None or timestamp.utcoffset() is None:
                raise IntegrityError("rebuild audit timestamp must be timezone-aware")
            normalized = (
                timestamp.astimezone(timezone.utc)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
            if normalized != timestamp_raw:
                raise IntegrityError("rebuild audit timestamp is not canonical UTC")
            timestamp_utc = timestamp.astimezone(timezone.utc)
            if previous_timestamp is not None and timestamp_utc < previous_timestamp:
                raise IntegrityError("rebuild audit timestamps are not monotonic")
            previous_timestamp = timestamp_utc
            timestamps.append(timestamp_raw)
        with self.transaction() as connection:
            connection.execute("DROP TRIGGER prevent_audit_update")
            connection.executemany(
                "UPDATE audit_log SET timestamp_utc=? WHERE sequence=?",
                ((timestamp, index) for index, timestamp in enumerate(timestamps, start=1)),
            )
            connection.execute(
                """
                CREATE TRIGGER prevent_audit_update
                BEFORE UPDATE ON audit_log
                BEGIN SELECT RAISE(ABORT, 'audit_log is append-only'); END
                """
            )

    def _all_payloads(
        self, entity_type: str, *, page_size: int = 1000
    ) -> tuple[dict[str, Any], ...]:
        offset = 0
        result: list[dict[str, Any]] = []
        while True:
            page = self.list_payloads(entity_type, limit=page_size, offset=offset)
            if not page:
                return tuple(result)
            result.extend(page)
            offset += len(page)

    def export_state(self, *, include_quarantine: bool = True) -> dict[str, Any]:
        state: dict[str, Any] = {
            "schema_version": _SCHEMA_VERSION,
            "registry_revision": self.revision(),
        }
        for entity_type in _ENTITY_QUERIES:
            state[_ENTITY_STATE_KEYS[entity_type]] = list(self._all_payloads(entity_type))
        if include_quarantine:
            state["quarantine"] = [
                {key: value for key, value in entry.items() if key != "payload"}
                for entry in self.quarantine_entries()
            ]
        state["state_hash"] = canonical_sha256(
            {
                key: value
                for key, value in state.items()
                if key not in {"registry_revision", "state_hash"}
            }
        )
        return state

    def entity_state_hash(self) -> str:
        state = self.export_state(include_quarantine=True)
        return str(state["state_hash"])

    def verified_state_hash(self) -> str:
        state = self.export_state(include_quarantine=False)
        return str(state["state_hash"])

    def export_rebuild_bundle(self) -> dict[str, Any]:
        operations: list[dict[str, Any]] = []
        package_rows = self._connection.execute(
            """
            SELECT package_id, registered_revision, payload_json, content_hash
            FROM packages ORDER BY registered_revision, package_id
            """
        ).fetchall()
        for row in package_rows:
            value = self._decode_stored_payload("package", row["payload_json"], row["content_hash"])
            operations.append(
                {
                    "operation_type": "package",
                    "revision": int(row["registered_revision"]),
                    "entity_id": str(row["package_id"]),
                    "payload": value,
                }
            )
        annotation_rows = self._connection.execute(
            "SELECT payload_json, payload_hash FROM registry_annotations "
            "ORDER BY new_revision, annotation_id"
        ).fetchall()
        for row in annotation_rows:
            value = self._decode_stored_payload(
                "annotation", row["payload_json"], row["payload_hash"]
            )
            operations.append(
                {
                    "operation_type": "annotation",
                    "revision": int(value["new_revision"]),
                    "entity_id": str(value["annotation_id"]),
                    "payload": value,
                }
            )
        for entry in self.quarantine_entries():
            operations.append(
                {
                    "operation_type": "quarantine",
                    "revision": int(entry["revision"]),
                    "entity_id": str(entry["quarantine_id"]),
                    "payload": entry,
                }
            )
        operations.sort(key=lambda item: (int(item["revision"]), str(item["operation_type"])))
        revisions = [int(item["revision"]) for item in operations]
        if revisions != list(range(1, self.revision() + 1)):
            raise IntegrityError("registry revisions cannot be represented as rebuild operations")
        audit_rows = self._connection.execute(
            "SELECT * FROM audit_log ORDER BY sequence"
        ).fetchall()
        audit_events = [dict(row) for row in audit_rows]
        integrity = self.verify_integrity()
        required_integrity_flags = (
            "audit_sequence_contiguous",
            "audit_revision_monotonic",
            "audit_revision_not_ahead",
            "audit_revision_coverage",
            "audit_event_ids_valid",
            "audit_bindings_valid",
            "audit_timestamps_valid",
            "audit_timestamp_monotonic",
            "operation_revision_coverage",
            "quarantine_records_valid",
            "entity_records_valid",
            "relation_records_valid",
            "annotation_records_valid",
            "annotation_actor_scope_constraint",
            "migration_records_valid",
            "append_only_triggers_present",
            "registered_revisions_valid",
        )
        if (
            integrity["sqlite_integrity"] != "ok"
            or integrity["foreign_key_violations"] != 0
            or any(integrity[field] is not True for field in required_integrity_flags)
        ):
            raise IntegrityError("registry integrity failed; rebuild bundle export is forbidden")
        bundle = {
            "schema_id": "modular-results-registry.rebuild-bundle",
            "schema_version": "4",
            "registry_schema_version": _SCHEMA_VERSION,
            "operations": operations,
            "audit_events": audit_events,
            "expected_registry_revision": self.revision(),
            "expected_verified_state_hash": self.verified_state_hash(),
            "expected_entity_state_hash": self.entity_state_hash(),
            "expected_audit_chain_hash": integrity["audit_chain_hash"],
        }
        bundle["bundle_hash"] = canonical_sha256(bundle)
        return bundle
