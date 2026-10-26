from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

from .canonical import canonical_sha256, deterministic_id
from .errors import RegistryError
from .storage_common import (
    _ENTITY_QUERIES,
    _ENTITY_RECORD_SPECS,
    _decode_canonical_json_object,
)

from .storage_mixin_base import StorageMixinBase


class StorageIntegrityRecordsMixin(StorageMixinBase):
    def _entity_records_valid(self) -> bool:
        try:
            for table, (
                id_column,
                payload_id_key,
                mirrored_columns,
            ) in _ENTITY_RECORD_SPECS.items():
                columns = (id_column, *mirrored_columns, "content_hash", "payload_json")
                rows = self._connection.execute(
                    "SELECT " + ",".join(columns) + " FROM " + table
                ).fetchall()
                for row in rows:
                    payload, payload_bytes = _decode_canonical_json_object(
                        row["payload_json"], context=f"{table} payload"
                    )
                    if canonical_sha256(payload) != str(row["content_hash"]):
                        return False
                    if payload_id_key is not None:
                        if payload.get(payload_id_key) != row[id_column]:
                            return False
                    elif table == "metric_definitions":
                        expected_id = deterministic_id(
                            "metricdef",
                            {
                                "metric_name": payload.get("metric_name"),
                                "definition_version": payload.get("definition_version"),
                            },
                            digest_chars=48,
                        )
                        if expected_id != str(row[id_column]):
                            return False
                    elif table == "metrics":
                        expected_id = deterministic_id(
                            "metric",
                            {
                                "run_id": payload.get("run_id"),
                                "metric_name": payload.get("metric_name"),
                                "definition_version": payload.get("definition_version"),
                                "split": payload.get("split"),
                                "period": payload.get("period"),
                                "scenario": payload.get("scenario"),
                            },
                            digest_chars=48,
                        )
                        if expected_id != str(row[id_column]):
                            return False
                        expected_definition_id = deterministic_id(
                            "metricdef",
                            {
                                "metric_name": payload.get("metric_name"),
                                "definition_version": payload.get("definition_version"),
                            },
                            digest_chars=48,
                        )
                        definition_row = self._connection.execute(
                            "SELECT definition_id FROM metrics WHERE metric_key=?",
                            (row[id_column],),
                        ).fetchone()
                        if (
                            definition_row is None
                            or str(definition_row[0]) != expected_definition_id
                        ):
                            return False
                    elif table == "experiment_roles":
                        if deterministic_id("role", payload, digest_chars=48) != str(
                            row[id_column]
                        ):
                            return False
                    for column in mirrored_columns:
                        if payload.get(column) != row[column]:
                            return False
            return True
        except (RegistryError, ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return False

    def _relation_records_valid(self) -> bool:
        try:
            run_rows = self._connection.execute(
                "SELECT run_id, payload_json FROM runs ORDER BY run_id"
            ).fetchall()
            for row in run_rows:
                payload, _ = _decode_canonical_json_object(
                    row["payload_json"], context="run relation payload"
                )
                datasets = tuple(
                    str(item[0])
                    for item in self._connection.execute(
                        "SELECT dataset_id FROM run_datasets WHERE run_id=? ORDER BY dataset_id",
                        (row["run_id"],),
                    ).fetchall()
                )
                artifacts = tuple(
                    str(item[0])
                    for item in self._connection.execute(
                        "SELECT artifact_id FROM run_artifacts WHERE run_id=? ORDER BY artifact_id",
                        (row["run_id"],),
                    ).fetchall()
                )
                if datasets != tuple(sorted(str(item) for item in payload["dataset_ids"])):
                    return False
                if artifacts != tuple(sorted(str(item) for item in payload["artifact_ids"])):
                    return False
            metric_rows = self._connection.execute(
                """
                    SELECT m.metric_key, m.definition_id, m.payload_json AS metric_payload,
                           d.payload_json AS definition_payload
                    FROM metrics AS m
                    JOIN metric_definitions AS d ON d.definition_id = m.definition_id
                    ORDER BY m.metric_key
                    """
            ).fetchall()
            for row in metric_rows:
                metric_payload, _ = _decode_canonical_json_object(
                    row["metric_payload"], context="metric relation payload"
                )
                definition_payload, _ = _decode_canonical_json_object(
                    row["definition_payload"],
                    context="metric definition relation payload",
                )
                expected_definition_id = deterministic_id(
                    "metricdef",
                    {
                        "metric_name": metric_payload.get("metric_name"),
                        "definition_version": metric_payload.get("definition_version"),
                    },
                    digest_chars=48,
                )
                if str(row["definition_id"]) != expected_definition_id:
                    return False
                if metric_payload.get("unit") != definition_payload.get("unit"):
                    return False
                if metric_payload.get("precision") != definition_payload.get("precision"):
                    return False
                if metric_payload.get("producer") != definition_payload.get("producer"):
                    return False

            evidence_rows = self._connection.execute(
                "SELECT evidence_id, payload_json FROM evidence ORDER BY evidence_id"
            ).fetchall()
            for row in evidence_rows:
                payload, _ = _decode_canonical_json_object(
                    row["payload_json"], context="evidence relation payload"
                )
                runs = tuple(
                    str(item[0])
                    for item in self._connection.execute(
                        "SELECT run_id FROM evidence_runs WHERE evidence_id=? ORDER BY run_id",
                        (row["evidence_id"],),
                    ).fetchall()
                )
                artifacts = tuple(
                    str(item[0])
                    for item in self._connection.execute(
                        "SELECT artifact_id FROM evidence_artifacts WHERE evidence_id=? ORDER BY artifact_id",
                        (row["evidence_id"],),
                    ).fetchall()
                )
                if runs != tuple(sorted(str(item) for item in payload["run_ids"])):
                    return False
                if artifacts != tuple(sorted(str(item) for item in payload["artifact_ids"])):
                    return False
            return True
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return False

    def _annotation_actor_scope_constraint_valid(self) -> bool:
        try:
            index_rows = self._connection.execute(
                "PRAGMA index_list(registry_annotations)"
            ).fetchall()
            expected_scope = ("entity_type", "entity_id", "actor", "reason", "payload_hash")
            expected_primary_key = ("annotation_id",)
            observed_unique: list[tuple[str, tuple[str, ...], bool]] = []
            for row in index_rows:
                if not bool(int(row["unique"])):
                    continue
                index_name = str(row["name"])
                columns = tuple(
                    str(item["name"])
                    for item in self._connection.execute(
                        "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                        (index_name,),
                    ).fetchall()
                )
                observed_unique.append((str(row["origin"]), columns, bool(int(row["partial"]))))
            return sorted(observed_unique) == sorted(
                [
                    ("pk", expected_primary_key, False),
                    ("u", expected_scope, False),
                ]
            )
        except (KeyError, TypeError, ValueError, sqlite3.DatabaseError):
            return False

    def _annotation_records_valid(self, current_revision: int) -> bool:
        rows = self._connection.execute(
            "SELECT * FROM registry_annotations ORDER BY new_revision, annotation_id"
        ).fetchall()
        required_document_fields = {
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
        try:
            for row in rows:
                document, payload_bytes = _decode_canonical_json_object(
                    row["payload_json"], context="annotation payload"
                )
                if set(document) != required_document_fields:
                    return False
                for column in (
                    "annotation_id",
                    "entity_type",
                    "entity_id",
                    "actor",
                    "reason",
                    "previous_revision",
                    "new_revision",
                    "entity_hash",
                    "payload_hash",
                    "created_at_utc",
                ):
                    if document[column] != row[column]:
                        return False
                previous_revision = int(row["previous_revision"])
                new_revision = int(row["new_revision"])
                if new_revision != previous_revision + 1 or not (
                    1 <= new_revision <= current_revision
                ):
                    return False
                payload_hash = canonical_sha256(document["payload"])
                if payload_hash != str(row["payload_hash"]):
                    return False
                entity_type = str(row["entity_type"])
                entity_id = str(row["entity_id"])
                if entity_type not in _ENTITY_QUERIES or entity_type in {"annotation", "package"}:
                    return False
                table, id_column = _ENTITY_QUERIES[entity_type]
                current_hash = self._existing_hash(
                    self._connection,
                    table=table,
                    id_column=id_column,
                    entity_id=entity_id,
                )
                if current_hash != str(row["entity_hash"]):
                    return False
                expected_id = deterministic_id(
                    "annotation",
                    {
                        "entity_type": entity_type,
                        "entity_id": entity_id,
                        "actor": str(row["actor"]),
                        "reason": str(row["reason"]),
                        "payload_hash": payload_hash,
                        "previous_revision": previous_revision,
                    },
                    digest_chars=48,
                )
                if expected_id != str(row["annotation_id"]):
                    return False
                created_raw = str(row["created_at_utc"])
                created = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                normalized = (
                    created.astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                if normalized != created_raw:
                    return False
            return True
        except (
            RegistryError,
            KeyError,
            TypeError,
            ValueError,
            UnicodeDecodeError,
            json.JSONDecodeError,
        ):
            return False

    def _migration_records_valid(self) -> bool:
        rows = self._connection.execute("SELECT * FROM schema_migrations ORDER BY rowid").fetchall()
        try:
            for row in rows:
                migration_id = str(row["migration_id"])
                source = int(row["source_version"])
                target = int(row["target_version"])
                if not migration_id or source < 0 or target <= source:
                    return False
                expected_hash = canonical_sha256(
                    {
                        "migration_id": migration_id,
                        "source_version": source,
                        "target_version": target,
                    }
                )
                if expected_hash != str(row["report_hash"]):
                    return False
                timestamp_raw = str(row["applied_at_utc"])
                timestamp = datetime.fromisoformat(timestamp_raw.replace("Z", "+00:00"))
                normalized = (
                    timestamp.astimezone(timezone.utc)
                    .isoformat(timespec="microseconds")
                    .replace("+00:00", "Z")
                )
                if normalized != timestamp_raw:
                    return False
            return bool(rows)
        except (TypeError, ValueError):
            return False
