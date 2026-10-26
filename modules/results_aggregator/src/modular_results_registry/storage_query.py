from __future__ import annotations

import hashlib
import unicodedata
from datetime import datetime, timezone
from typing import Any, Mapping

from .canonical import canonical_json_bytes, canonical_sha256, deterministic_id
from .errors import IntegrityError, ResourceLimitError
from .storage_common import (
    _ENTITY_QUERIES,
    _FILTER_COLUMNS,
    _MAX_ANNOTATION_ACTOR_BYTES,
    _MAX_ANNOTATION_PAYLOAD_BYTES,
    _MAX_ANNOTATION_REASON_BYTES,
    _SHA256_HEX,
    _decode_canonical_json_object,
    _sql_identifier,
)

from .storage_mixin_base import StorageMixinBase


class StorageQueryMixin(StorageMixinBase):
    @staticmethod
    def _decode_stored_payload(
        entity_type: str, payload_raw: object, stored_hash: object
    ) -> dict[str, Any]:
        value, payload_bytes = _decode_canonical_json_object(payload_raw, context="stored payload")
        if (
            not isinstance(stored_hash, str)
            or len(stored_hash) != 64
            or any(character not in _SHA256_HEX for character in stored_hash)
        ):
            raise IntegrityError("stored payload hash column is invalid")
        if entity_type == "annotation":
            required = {
                "annotation_id",
                "entity_type",
                "entity_id",
                "actor",
                "reason",
                "previous_revision",
                "new_revision",
                "entity_hash",
                "payload_hash",
                "payload",
                "created_at_utc",
            }
            if set(value) != required:
                raise IntegrityError("stored annotation payload fields are invalid")
            if canonical_sha256(value["payload"]) != value["payload_hash"]:
                raise IntegrityError("stored annotation payload hash mismatch")
            if value["payload_hash"] != stored_hash:
                raise IntegrityError("stored annotation hash column mismatch")
            expected_id = deterministic_id(
                "annotation",
                {
                    "entity_type": value["entity_type"],
                    "entity_id": value["entity_id"],
                    "actor": value["actor"],
                    "reason": value["reason"],
                    "payload_hash": value["payload_hash"],
                    "previous_revision": value["previous_revision"],
                },
                digest_chars=48,
            )
            if expected_id != value["annotation_id"]:
                raise IntegrityError("stored annotation ID mismatch")
        elif canonical_sha256(value) != stored_hash:
            raise IntegrityError("stored payload content hash mismatch")
        return value

    @staticmethod
    def _payload_select(entity_type: str) -> str:
        if entity_type == "annotation":
            return "payload_json,payload_hash AS stored_hash"
        return "payload_json,content_hash AS stored_hash"

    def get_payload(self, entity_type: str, entity_id: str) -> dict[str, Any] | None:
        if not isinstance(entity_type, str) or entity_type not in _ENTITY_QUERIES:
            raise ValueError(f"unsupported entity type {entity_type!r}")
        if not isinstance(entity_id, str) or not entity_id or entity_id != entity_id.strip():
            raise ValueError("entity_id must be a canonical non-empty string")
        table, id_column = _ENTITY_QUERIES[entity_type]
        query = (
            "SELECT "
            + self._payload_select(entity_type)
            + " FROM "
            + _sql_identifier(table)
            + " WHERE "
            + _sql_identifier(id_column)
            + "=?"
        )
        row = self._connection.execute(query, (entity_id,)).fetchone()
        if row is None:
            return None
        return self._decode_stored_payload(entity_type, row["payload_json"], row["stored_hash"])

    def list_payloads(
        self,
        entity_type: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(entity_type, str) or entity_type not in _ENTITY_QUERIES:
            raise ValueError(f"unsupported entity type {entity_type!r}")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or limit < 1
            or limit > 10_000
            or offset < 0
        ):
            raise ValueError("invalid pagination")
        table, id_column = _ENTITY_QUERIES[entity_type]
        query = (
            "SELECT "
            + self._payload_select(entity_type)
            + " FROM "
            + _sql_identifier(table)
            + " ORDER BY "
            + _sql_identifier(id_column)
            + " LIMIT ? OFFSET ?"
        )
        rows = self._connection.execute(query, (limit, offset)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                self._decode_stored_payload(entity_type, row["payload_json"], row["stored_hash"])
            )
        return tuple(result)

    def query_payloads(
        self,
        entity_type: str,
        *,
        filters: Mapping[str, str | None],
        limit: int = 100,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        if not isinstance(entity_type, str) or entity_type not in _ENTITY_QUERIES:
            raise ValueError(f"unsupported entity type {entity_type!r}")
        if not isinstance(filters, Mapping):
            raise ValueError("filters must be a mapping")
        # One extra row is permitted for read-only cursor detection by QueryAPI.
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or limit < 1
            or limit > 10_001
            or offset < 0
        ):
            raise ValueError("invalid pagination")
        allowed = _FILTER_COLUMNS[entity_type]
        if any(not isinstance(column, str) for column in filters):
            raise ValueError("filter names must be strings")
        unknown = set(filters) - allowed
        if unknown:
            raise ValueError(f"unsupported filters for {entity_type}: {sorted(unknown)}")
        table, id_column = _ENTITY_QUERIES[entity_type]
        clauses: list[str] = []
        values: list[Any] = []
        for column in sorted(filters):
            value = filters[column]
            if value is None:
                clauses.append(_sql_identifier(column) + " IS NULL")
            else:
                if not isinstance(value, str) or not value:
                    raise ValueError("filter values must be non-empty strings or None")
                clauses.append(_sql_identifier(column) + "=?")
                values.append(value)
        where = "" if not clauses else " WHERE " + " AND ".join(clauses)
        query = (
            "SELECT "
            + self._payload_select(entity_type)
            + " FROM "
            + _sql_identifier(table)
            + where
            + " ORDER BY "
            + _sql_identifier(id_column)
            + " LIMIT ? OFFSET ?"
        )
        rows = self._connection.execute(query, (*values, limit, offset)).fetchall()
        result: list[dict[str, Any]] = []
        for row in rows:
            result.append(
                self._decode_stored_payload(entity_type, row["payload_json"], row["stored_hash"])
            )
        return tuple(result)

    def add_annotation(
        self,
        *,
        entity_type: str,
        entity_id: str,
        actor: str,
        reason: str,
        payload: Mapping[str, Any],
        created_at_utc: str | None = None,
    ) -> tuple[str, str, int]:
        if entity_type not in _ENTITY_QUERIES or entity_type in {"annotation", "package"}:
            raise ValueError("unsupported annotation entity type")
        for name, value, maximum in (
            ("actor", actor, _MAX_ANNOTATION_ACTOR_BYTES),
            ("reason", reason, _MAX_ANNOTATION_REASON_BYTES),
        ):
            if not isinstance(value, str) or not value or value != value.strip():
                raise ValueError(f"annotation {name} must be canonical non-empty text")
            if unicodedata.normalize("NFC", value) != value:
                raise ValueError(f"annotation {name} must use NFC Unicode normalization")
            if any(
                ord(character) < 32 or ord(character) == 127 or character in {"\u2028", "\u2029"}
                for character in value
            ):
                raise ValueError(f"annotation {name} cannot contain control characters")
            if len(value.encode("utf-8")) > maximum:
                raise ResourceLimitError(f"annotation {name} exceeds its byte limit")
        if not isinstance(payload, Mapping):
            raise TypeError("annotation payload must be a mapping")
        table, id_column = _ENTITY_QUERIES[entity_type]
        query = (
            "SELECT content_hash FROM "
            + _sql_identifier(table)
            + " WHERE "
            + _sql_identifier(id_column)
            + "=?"
        )
        payload_bytes = canonical_json_bytes(payload)
        if len(payload_bytes) > _MAX_ANNOTATION_PAYLOAD_BYTES:
            raise ResourceLimitError("annotation payload exceeds its byte limit")
        payload_hash = hashlib.sha256(payload_bytes).hexdigest()
        # Entity lookup and idempotency lookup must be serialized with revision
        # allocation. A pre-transaction check permits two writers to both observe
        # absence and turns an otherwise idempotent retry into a raw UNIQUE error.
        with self.transaction() as connection:
            row = connection.execute(query, (entity_id,)).fetchone()
            if row is None:
                raise IntegrityError("cannot annotate a missing entity")
            entity_hash = str(row[0])
            existing = connection.execute(
                """
                SELECT annotation_id, new_revision FROM registry_annotations
                WHERE entity_type=? AND entity_id=? AND actor=? AND reason=? AND payload_hash=?
                """,
                (entity_type, entity_id, actor, reason, payload_hash),
            ).fetchone()
            if existing is not None:
                return str(existing[0]), "DUPLICATE", int(existing[1])
            previous = self.revision()
            revision = self._next_revision(connection)
            annotation_id = deterministic_id(
                "annotation",
                {
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "actor": actor,
                    "reason": reason,
                    "payload_hash": payload_hash,
                    "previous_revision": previous,
                },
                digest_chars=48,
            )
            if created_at_utc is None:
                created_at = (
                    datetime.now(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
            else:
                try:
                    parsed_created_at = datetime.fromisoformat(
                        created_at_utc.replace("Z", "+00:00")
                    )
                except ValueError as exc:
                    raise ValueError("annotation created_at_utc is invalid") from exc
                if parsed_created_at.tzinfo is None or parsed_created_at.utcoffset() is None:
                    raise ValueError("annotation created_at_utc must be timezone-aware")
                created_at = (
                    parsed_created_at.astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
            annotation_document = canonical_json_bytes(
                {
                    "annotation_id": annotation_id,
                    "entity_type": entity_type,
                    "entity_id": entity_id,
                    "actor": actor,
                    "reason": reason,
                    "previous_revision": previous,
                    "new_revision": revision,
                    "entity_hash": entity_hash,
                    "payload_hash": payload_hash,
                    "payload": payload,
                    "created_at_utc": created_at,
                }
            )
            connection.execute(
                """
                INSERT INTO registry_annotations(
                    annotation_id, entity_type, entity_id, actor, reason,
                    previous_revision, new_revision, entity_hash, payload_hash,
                    payload_json, created_at_utc
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    annotation_id,
                    entity_type,
                    entity_id,
                    actor,
                    reason,
                    previous,
                    revision,
                    entity_hash,
                    payload_hash,
                    annotation_document,
                    created_at,
                ),
            )
            self._audit(
                connection,
                revision=revision,
                operation="ANNOTATED",
                entity_type=entity_type,
                entity_id=entity_id,
                previous_state="REGISTERED",
                new_state="ANNOTATED",
                reason_code=reason,
                correlation_id=annotation_id,
                payload_hash=payload_hash,
            )
        return annotation_id, "REGISTERED", revision

    def annotations(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[dict[str, Any], ...]:
        filters: dict[str, str] = {}
        if entity_type is not None:
            filters["entity_type"] = entity_type
        if entity_id is not None:
            filters["entity_id"] = entity_id
        return self.query_payloads("annotation", filters=filters, limit=limit, offset=offset)

    def audit_events(self, *, limit: int = 1000, offset: int = 0) -> tuple[dict[str, Any], ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or isinstance(offset, bool)
            or not isinstance(offset, int)
            or limit < 1
            or limit > 100_000
            or offset < 0
        ):
            raise ValueError("invalid pagination")
        rows = self._connection.execute(
            "SELECT * FROM audit_log ORDER BY sequence LIMIT ? OFFSET ?",
            (limit, offset),
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def quarantine_count(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) FROM quarantine").fetchone()
        return 0 if row is None else int(row[0])

    def quarantine_entries(self) -> tuple[dict[str, Any], ...]:
        rows = self._connection.execute(
            """
            SELECT quarantine_id, entity_type, entity_id, existing_content_hash,
                   incoming_content_hash, payload_json, reason_code, correlation_id,
                   revision, created_at_utc
            FROM quarantine ORDER BY revision, quarantine_id
            """
        ).fetchall()
        entries: list[dict[str, Any]] = []
        for row in rows:
            payload, payload_bytes = _decode_canonical_json_object(
                row["payload_json"], context="quarantine payload"
            )
            if canonical_sha256(payload) != str(row["incoming_content_hash"]):
                raise IntegrityError("quarantine payload does not match incoming_content_hash")
            entries.append(
                {
                    "quarantine_id": str(row["quarantine_id"]),
                    "entity_type": str(row["entity_type"]),
                    "entity_id": str(row["entity_id"]),
                    "existing_content_hash": str(row["existing_content_hash"]),
                    "incoming_content_hash": str(row["incoming_content_hash"]),
                    "payload": payload,
                    "payload_bytes_sha256": hashlib.sha256(payload_bytes).hexdigest(),
                    "reason_code": str(row["reason_code"]),
                    "correlation_id": str(row["correlation_id"]),
                    "revision": int(row["revision"]),
                    "created_at_utc": str(row["created_at_utc"]),
                }
            )
        return tuple(entries)
