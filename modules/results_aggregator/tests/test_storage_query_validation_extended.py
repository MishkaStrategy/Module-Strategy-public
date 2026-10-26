from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import make_package
from modular_results_registry.canonical import (
    canonical_json_bytes,
    canonical_sha256,
    deterministic_id,
)
from modular_results_registry.errors import ConflictError, IntegrityError, ResourceLimitError
from modular_results_registry.storage import SQLiteRegistry
from modular_results_registry.storage_query import StorageQueryMixin


def _annotation_document(
    *, payload: dict[str, object] | None = None
) -> tuple[dict[str, object], str]:
    payload = {"text": "note"} if payload is None else payload
    payload_hash = canonical_sha256(payload)
    base: dict[str, object] = {
        "entity_type": "experiment",
        "entity_id": "experiment-001",
        "actor": "auditor",
        "reason": "LIMITATION",
        "previous_revision": 1,
        "new_revision": 2,
        "entity_hash": "a" * 64,
        "payload_hash": payload_hash,
        "payload": payload,
        "created_at_utc": "2026-07-23T08:00:00.000000Z",
    }
    base["annotation_id"] = deterministic_id(
        "annotation",
        {
            "entity_type": base["entity_type"],
            "entity_id": base["entity_id"],
            "actor": base["actor"],
            "reason": base["reason"],
            "payload_hash": payload_hash,
            "previous_revision": base["previous_revision"],
        },
        digest_chars=48,
    )
    return base, payload_hash


def test_decode_stored_payload_validates_hashes_and_annotation_identity() -> None:
    value = {"id": "x", "value": 1}
    stored_hash = canonical_sha256(value)
    assert (
        StorageQueryMixin._decode_stored_payload(
            "experiment", canonical_json_bytes(value), stored_hash
        )
        == value
    )
    for bad_hash in (None, 1, "a", "A" * 64, "g" * 64):
        with pytest.raises(IntegrityError, match="hash column is invalid"):
            StorageQueryMixin._decode_stored_payload(
                "experiment", canonical_json_bytes(value), bad_hash
            )
    with pytest.raises(IntegrityError, match="content hash mismatch"):
        StorageQueryMixin._decode_stored_payload(
            "experiment", canonical_json_bytes(value), "0" * 64
        )

    document, payload_hash = _annotation_document()
    assert (
        StorageQueryMixin._decode_stored_payload(
            "annotation", canonical_json_bytes(document), payload_hash
        )
        == document
    )

    bad = dict(document)
    bad["extra"] = True
    with pytest.raises(IntegrityError, match="fields are invalid"):
        StorageQueryMixin._decode_stored_payload(
            "annotation", canonical_json_bytes(bad), payload_hash
        )
    bad = dict(document)
    bad["payload"] = {"text": "changed"}
    with pytest.raises(IntegrityError, match="payload hash mismatch"):
        StorageQueryMixin._decode_stored_payload(
            "annotation", canonical_json_bytes(bad), payload_hash
        )
    with pytest.raises(IntegrityError, match="hash column mismatch"):
        StorageQueryMixin._decode_stored_payload(
            "annotation", canonical_json_bytes(document), "0" * 64
        )
    bad = dict(document)
    bad["annotation_id"] = "annotation-forged"
    with pytest.raises(IntegrityError, match="ID mismatch"):
        StorageQueryMixin._decode_stored_payload(
            "annotation", canonical_json_bytes(bad), payload_hash
        )


def test_storage_query_validation_and_null_filters(tmp_path: Path) -> None:
    package = make_package()
    with SQLiteRegistry(tmp_path / "registry.sqlite") as registry:
        registry.register_package(package)
        for entity_type in (None, 1, "unknown"):
            with pytest.raises(ValueError, match="unsupported entity type"):
                registry.get_payload(entity_type, "x")  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="unsupported entity type"):
                registry.list_payloads(entity_type)  # type: ignore[arg-type]
            with pytest.raises(ValueError, match="unsupported entity type"):
                registry.query_payloads(entity_type, filters={})  # type: ignore[arg-type]
        for entity_id in (None, 1, "", " x "):
            with pytest.raises(ValueError, match="entity_id"):
                registry.get_payload("experiment", entity_id)  # type: ignore[arg-type]

        for limit, offset in (
            (True, 0),
            (1.5, 0),
            (1, True),
            (1, 1.5),
            (0, 0),
            (10_001, 0),
            (1, -1),
        ):
            with pytest.raises(ValueError, match="pagination"):
                registry.list_payloads("experiment", limit=limit, offset=offset)  # type: ignore[arg-type]
        for limit, offset in (
            (True, 0),
            (1.5, 0),
            (1, True),
            (1, 1.5),
            (0, 0),
            (10_002, 0),
            (1, -1),
        ):
            with pytest.raises(ValueError, match="pagination"):
                registry.query_payloads("experiment", filters={}, limit=limit, offset=offset)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="filters must be a mapping"):
            registry.query_payloads("experiment", filters=[])  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="filter names must be strings"):
            registry.query_payloads("experiment", filters={1: "x"})  # type: ignore[dict-item]
        with pytest.raises(ValueError, match="unsupported filters"):
            registry.query_payloads("experiment", filters={"sha256": "x"})
        for value in ("", 1, False):
            with pytest.raises(ValueError, match="filter values"):
                registry.query_payloads("experiment", filters={"strategy_id": value})  # type: ignore[dict-item]

        assert (
            registry.query_payloads("experiment", filters={"parent_experiment_id": None})[0][
                "experiment_id"
            ]
            == package.experiments[0].experiment_id
        )
        assert registry.list_payloads("experiment", limit=1, offset=999) == ()
        assert registry.get_payload("experiment", "missing") is None


def test_annotation_validation_idempotency_and_timestamp_normalization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import modular_results_registry.storage_query as storage_query

    package = make_package()
    with SQLiteRegistry(tmp_path / "registry.sqlite") as registry:
        registry.register_package(package)
        experiment_id = package.experiments[0].experiment_id
        for entity_type in ("annotation", "package", "unknown", None):
            with pytest.raises(ValueError, match="unsupported annotation"):
                registry.add_annotation(
                    entity_type=entity_type,  # type: ignore[arg-type]
                    entity_id=experiment_id,
                    actor="auditor",
                    reason="LIMITATION",
                    payload={"x": 1},
                )
        for field, value in (
            ("actor", None),
            ("actor", ""),
            ("actor", " auditor "),
            ("reason", 1),
            ("reason", ""),
            ("reason", "LIMIT\nATION"),
            ("reason", "LIMIT\u2028ATION"),
        ):
            kwargs = {
                "entity_type": "experiment",
                "entity_id": experiment_id,
                "actor": "auditor",
                "reason": "LIMITATION",
                "payload": {"x": 1},
            }
            kwargs[field] = value
            with pytest.raises(ValueError, match="annotation"):
                registry.add_annotation(**kwargs)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="NFC"):
            registry.add_annotation(
                entity_type="experiment",
                entity_id=experiment_id,
                actor="e\u0301",
                reason="LIMITATION",
                payload={"x": 1},
            )
        monkeypatch.setattr(storage_query, "_MAX_ANNOTATION_ACTOR_BYTES", 3)
        with pytest.raises(ResourceLimitError, match="actor"):
            registry.add_annotation(
                entity_type="experiment",
                entity_id=experiment_id,
                actor="four",
                reason="LIMITATION",
                payload={"x": 1},
            )
        monkeypatch.setattr(storage_query, "_MAX_ANNOTATION_ACTOR_BYTES", 512)
        monkeypatch.setattr(storage_query, "_MAX_ANNOTATION_REASON_BYTES", 3)
        with pytest.raises(ResourceLimitError, match="reason"):
            registry.add_annotation(
                entity_type="experiment",
                entity_id=experiment_id,
                actor="auditor",
                reason="FOUR",
                payload={"x": 1},
            )
        monkeypatch.setattr(storage_query, "_MAX_ANNOTATION_REASON_BYTES", 2048)
        with pytest.raises(TypeError, match="payload must be a mapping"):
            registry.add_annotation(
                entity_type="experiment",
                entity_id=experiment_id,
                actor="auditor",
                reason="LIMITATION",
                payload=[],  # type: ignore[arg-type]
            )
        monkeypatch.setattr(storage_query, "_MAX_ANNOTATION_PAYLOAD_BYTES", 3)
        with pytest.raises(ResourceLimitError, match="payload"):
            registry.add_annotation(
                entity_type="experiment",
                entity_id=experiment_id,
                actor="auditor",
                reason="LIMITATION",
                payload={"long": "value"},
            )
        monkeypatch.setattr(storage_query, "_MAX_ANNOTATION_PAYLOAD_BYTES", 16 * 1024 * 1024)
        with pytest.raises(IntegrityError, match="missing entity"):
            registry.add_annotation(
                entity_type="experiment",
                entity_id="missing",
                actor="auditor",
                reason="LIMITATION",
                payload={"x": 1},
            )
        for timestamp, message in (
            ("invalid", "invalid"),
            ("2026-07-23T08:00:00", "timezone-aware"),
        ):
            with pytest.raises(ValueError, match=message):
                registry.add_annotation(
                    entity_type="experiment",
                    entity_id=experiment_id,
                    actor="auditor",
                    reason="LIMITATION",
                    payload={"timestamp": timestamp},
                    created_at_utc=timestamp,
                )

        annotation_id, state, revision = registry.add_annotation(
            entity_type="experiment",
            entity_id=experiment_id,
            actor="auditor",
            reason="LIMITATION",
            payload={"x": 1},
            created_at_utc="2026-07-23T10:00:00+02:00",
        )
        assert state == "REGISTERED"
        duplicate = registry.add_annotation(
            entity_type="experiment",
            entity_id=experiment_id,
            actor="auditor",
            reason="LIMITATION",
            payload={"x": 1},
            created_at_utc="2026-07-23T11:00:00+03:00",
        )
        assert duplicate == (annotation_id, "DUPLICATE", revision)
        stored = registry.get_payload("annotation", annotation_id)
        assert stored is not None
        assert stored["created_at_utc"] == "2026-07-23T08:00:00.000000Z"
        assert registry.annotations(entity_type="experiment", entity_id=experiment_id) == (stored,)


def test_audit_pagination_and_quarantine_hash_binding(tmp_path: Path) -> None:
    package = make_package()
    with SQLiteRegistry(tmp_path / "registry.sqlite") as registry:
        registry.register_package(package)
        for limit, offset in (
            (0, 0),
            (100_001, 0),
            (1, -1),
            (True, 0),
            (1, True),
            (1.5, 0),
            (1, 1.5),
        ):
            with pytest.raises(ValueError, match="pagination"):
                registry.audit_events(limit=limit, offset=offset)  # type: ignore[arg-type]
        assert registry.audit_events(limit=1)

        conflict = replace(package, metrics=(replace(package.metrics[0], value=9),))
        with pytest.raises(ConflictError):
            registry.register_package(conflict)
        assert registry.quarantine_count() == 1
        assert len(registry.quarantine_entries()) == 1
        registry._connection.execute("DROP TRIGGER prevent_quarantine_update")
        registry._connection.execute("UPDATE quarantine SET incoming_content_hash=?", ("0" * 64,))
        with pytest.raises(IntegrityError, match="incoming_content_hash"):
            registry.quarantine_entries()


def test_quarantine_count_none_branch() -> None:
    class Cursor:
        def fetchone(self):
            return None

    class Connection:
        def execute(self, *args, **kwargs):
            return Cursor()

    dummy = SimpleNamespace(_connection=Connection())
    assert StorageQueryMixin.quarantine_count(dummy) == 0
