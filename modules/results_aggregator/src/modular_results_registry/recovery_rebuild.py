from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from . import recovery
from .canonical import canonical_sha256
from .errors import RecoveryError, RegistryError
from .serialization import registration_package_from_mapping
from .storage import SQLiteRegistry


def rebuild_from_bundle(bundle: Mapping[str, Any], destination_path: str | Path) -> Path:
    if not isinstance(bundle, Mapping):
        raise RecoveryError("rebuild bundle must be an object")
    expected_fields = {
        "schema_id",
        "schema_version",
        "registry_schema_version",
        "operations",
        "audit_events",
        "expected_registry_revision",
        "expected_verified_state_hash",
        "expected_entity_state_hash",
        "expected_audit_chain_hash",
        "bundle_hash",
    }
    if set(bundle) != expected_fields:
        raise RecoveryError("rebuild bundle fields are invalid")
    expected_bundle_hash = bundle.get("bundle_hash")
    try:
        actual_bundle_hash = canonical_sha256(
            {key: value for key, value in bundle.items() if key != "bundle_hash"}
        )
    except RegistryError as exc:
        raise RecoveryError("rebuild bundle is not canonical JSON") from exc
    if expected_bundle_hash != actual_bundle_hash:
        raise RecoveryError("rebuild bundle hash mismatch")
    if bundle.get("schema_id") != "modular-results-registry.rebuild-bundle":
        raise RecoveryError("unsupported rebuild bundle schema")
    if bundle.get("schema_version") != "4":
        raise RecoveryError("unsupported rebuild bundle schema")
    if bundle.get("registry_schema_version") != 4:
        raise RecoveryError("unsupported rebuild registry schema")
    operations = bundle.get("operations")
    if not isinstance(operations, list):
        raise RecoveryError("rebuild bundle operations must be a list")
    audit_events = bundle.get("audit_events")
    if not isinstance(audit_events, list):
        raise RecoveryError("rebuild bundle audit_events must be a list")
    expected_revision_raw = bundle.get("expected_registry_revision")
    if (
        isinstance(expected_revision_raw, bool)
        or not isinstance(expected_revision_raw, int)
        or expected_revision_raw < 0
    ):
        raise RecoveryError("rebuild bundle registry revision is invalid")
    expected_revision = expected_revision_raw
    if len(operations) != expected_revision:
        raise RecoveryError("rebuild operation count does not match registry revision")
    for field in (
        "expected_verified_state_hash",
        "expected_entity_state_hash",
        "expected_audit_chain_hash",
        "bundle_hash",
    ):
        if not recovery._is_sha256(bundle.get(field)):
            raise RecoveryError(f"rebuild bundle {field} is invalid")
    expected_revisions: list[int] = []
    for raw in operations:
        if not isinstance(raw, Mapping):
            raise RecoveryError("rebuild operation entry must be an object")
        if set(raw) != {"operation_type", "revision", "entity_id", "payload"}:
            raise RecoveryError("rebuild operation fields are invalid")
        revision = raw.get("revision")
        if isinstance(revision, bool) or not isinstance(revision, int):
            raise RecoveryError("rebuild operation revision is invalid")
        operation_type = raw.get("operation_type")
        if operation_type not in {"package", "annotation", "quarantine"}:
            raise RecoveryError("unsupported rebuild operation type")
        entity_id = raw.get("entity_id")
        if not isinstance(entity_id, str) or not entity_id:
            raise RecoveryError("rebuild operation entity_id is invalid")
        expected_revisions.append(revision)
    if expected_revisions != list(range(1, expected_revision + 1)):
        raise RecoveryError("rebuild operations are not revision ordered")
    if any(not isinstance(event, Mapping) for event in audit_events):
        raise RecoveryError("rebuild audit event entry must be an object")

    destination = Path(destination_path)
    recovery._ensure_safe_parent(destination)
    if recovery._lexists(destination):
        raise RecoveryError("rebuild destination already exists")
    temp = destination.with_name(f".{destination.name}.rebuild-{os.getpid()}")
    if recovery._lexists(temp):
        raise RecoveryError("rebuild temporary path collision")
    try:
        with SQLiteRegistry(temp) as registry:
            for raw in operations:
                operation_type = str(raw["operation_type"])
                revision = int(raw["revision"])
                entity_id = str(raw["entity_id"])
                payload = raw["payload"]
                if not isinstance(payload, Mapping):
                    raise RecoveryError("rebuild operation payload must be an object")
                try:
                    if operation_type == "package":
                        package = registration_package_from_mapping(payload)
                        outcome = registry.register_package(package)
                        if (
                            outcome.state != "REGISTERED"
                            or outcome.registry_revision != revision
                            or package.package_id != entity_id
                        ):
                            raise RecoveryError("rebuild package identity mismatch")
                    elif operation_type == "annotation":
                        if set(payload) != recovery._ANNOTATION_DOCUMENT_FIELDS:
                            raise RecoveryError("rebuild annotation document fields are invalid")
                        annotation_payload = payload["payload"]
                        if not isinstance(annotation_payload, Mapping):
                            raise RecoveryError("rebuild annotation payload must be an object")
                        for field in (
                            "annotation_id",
                            "entity_type",
                            "entity_id",
                            "actor",
                            "reason",
                            "entity_hash",
                            "payload_hash",
                            "created_at_utc",
                        ):
                            if not isinstance(payload[field], str):
                                raise RecoveryError(f"rebuild annotation {field} must be a string")
                        for field in ("previous_revision", "new_revision"):
                            item = payload[field]
                            if isinstance(item, bool) or not isinstance(item, int):
                                raise RecoveryError(
                                    f"rebuild annotation {field} must be an integer"
                                )
                        if not recovery._is_sha256(
                            payload["entity_hash"]
                        ) or not recovery._is_sha256(payload["payload_hash"]):
                            raise RecoveryError("rebuild annotation hash is invalid")
                        annotation_id, state, actual_revision = registry.add_annotation(
                            entity_type=payload["entity_type"],
                            entity_id=payload["entity_id"],
                            actor=payload["actor"],
                            reason=payload["reason"],
                            payload=annotation_payload,
                            created_at_utc=payload["created_at_utc"],
                        )
                        if (
                            state != "REGISTERED"
                            or annotation_id != entity_id
                            or annotation_id != payload.get("annotation_id")
                            or actual_revision != revision
                        ):
                            raise RecoveryError("rebuild annotation identity mismatch")
                    elif operation_type == "quarantine":
                        actual_revision = registry._restore_quarantine_entry(payload)
                        if (
                            actual_revision != revision
                            or str(payload.get("quarantine_id")) != entity_id
                        ):
                            raise RecoveryError("rebuild quarantine identity mismatch")
                    else:
                        raise RecoveryError("unsupported rebuild operation type")
                except RecoveryError:
                    raise
                except (KeyError, TypeError, ValueError, RegistryError, sqlite3.Error) as exc:
                    raise RecoveryError(f"invalid rebuild {operation_type} operation") from exc

            if registry.revision() != expected_revision:
                raise RecoveryError("full rebuild registry revision mismatch")
            try:
                registry._restore_audit_timestamps(audit_events)
            except (RegistryError, sqlite3.Error) as exc:
                raise RecoveryError("invalid rebuild audit evidence") from exc

            if registry.verified_state_hash() != bundle.get("expected_verified_state_hash"):
                raise RecoveryError("full rebuild verified state hash mismatch")
            if registry.entity_state_hash() != bundle.get("expected_entity_state_hash"):
                raise RecoveryError("full rebuild entity state hash mismatch")
            integrity = registry.verify_integrity()
            if integrity["audit_chain_hash"] != bundle.get("expected_audit_chain_hash"):
                raise RecoveryError("full rebuild audit chain hash mismatch")
            if not recovery._strict_integrity_passes(integrity):
                raise RecoveryError("rebuilt registry integrity check failed")
        recovery._fsync_regular_file(temp)
        recovery._publish_temp_noreplace(temp, destination)
        return destination
    finally:
        temp.unlink(missing_ok=True)
