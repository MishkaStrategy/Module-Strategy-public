from __future__ import annotations

import os
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_package
from modular_results_registry.canonical import canonical_sha256
from modular_results_registry.errors import ConflictError, RecoveryError
from modular_results_registry.recovery import rebuild_from_bundle
from modular_results_registry.storage import SQLiteRegistry


def _bundle(tmp_path: Path, *, annotation: bool = False, quarantine: bool = False):
    path = tmp_path / "source.sqlite"
    with SQLiteRegistry(path) as registry:
        package = make_package()
        registry.register_package(package)
        if annotation:
            registry.add_annotation(
                entity_type="experiment",
                entity_id=package.experiments[0].experiment_id,
                actor="auditor",
                reason="LIMITATION",
                payload={"text": "synthetic"},
            )
        if quarantine:
            with pytest.raises(ConflictError):
                registry.register_package(replace(package, producer_module_version="conflicting"))
        return registry.export_rebuild_bundle()


def _rehash(bundle):
    value = dict(bundle)
    value["bundle_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "bundle_hash"}
    )
    return value


def test_rebuild_round_trips_quarantine_operation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, quarantine=True)
    assert [item["operation_type"] for item in bundle["operations"]] == [
        "package",
        "quarantine",
    ]
    destination = tmp_path / "rebuilt.sqlite"
    rebuild_from_bundle(bundle, destination)
    with SQLiteRegistry(destination) as registry:
        assert registry.revision() == 2
        assert registry.quarantine_count() == 1
        assert registry.verified_state_hash() == bundle["expected_verified_state_hash"]
        assert registry.verify_integrity()["quarantine_records_valid"] is True


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (lambda b: [], "must be an object"),
        (
            lambda b: {key: value for key, value in b.items() if key != "schema_id"},
            "fields are invalid",
        ),
        (lambda b: {**b, "bundle_hash": "0" * 64}, "hash mismatch"),
        (lambda b: _rehash({**b, "schema_id": "wrong"}), "unsupported rebuild bundle schema"),
        (lambda b: _rehash({**b, "schema_version": "3"}), "unsupported rebuild bundle schema"),
        (
            lambda b: _rehash({**b, "registry_schema_version": 3}),
            "unsupported rebuild registry schema",
        ),
        (lambda b: _rehash({**b, "operations": {}}), "operations must be a list"),
        (lambda b: _rehash({**b, "audit_events": {}}), "audit_events must be a list"),
        (
            lambda b: _rehash({**b, "expected_registry_revision": True}),
            "registry revision is invalid",
        ),
        (
            lambda b: _rehash({**b, "expected_registry_revision": -1}),
            "registry revision is invalid",
        ),
        (lambda b: _rehash({**b, "expected_registry_revision": 2}), "operation count"),
        (
            lambda b: _rehash({**b, "expected_entity_state_hash": "x"}),
            "expected_entity_state_hash is invalid",
        ),
        (lambda b: _rehash({**b, "operations": [1]}), "operation entry must be an object"),
        (lambda b: _rehash({**b, "operations": [{"revision": 1}]}), "operation fields are invalid"),
        (
            lambda b: _rehash({**b, "operations": [{**b["operations"][0], "revision": True}]}),
            "operation revision is invalid",
        ),
        (
            lambda b: _rehash(
                {**b, "operations": [{**b["operations"][0], "operation_type": "bad"}]}
            ),
            "unsupported rebuild operation type",
        ),
        (
            lambda b: _rehash({**b, "operations": [{**b["operations"][0], "entity_id": ""}]}),
            "entity_id is invalid",
        ),
        (lambda b: _rehash({**b, "audit_events": [1]}), "audit event entry must be an object"),
    ],
)
def test_rebuild_top_level_validation(tmp_path: Path, mutator, message: str) -> None:
    bundle = _bundle(tmp_path)
    broken = mutator(bundle)
    with pytest.raises(RecoveryError, match=message):
        rebuild_from_bundle(broken, tmp_path / "destination.sqlite")


def test_rebuild_rejects_noncanonical_bundle_value(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    broken = dict(bundle)
    broken["operations"] = [object()]
    with pytest.raises(RecoveryError, match="not canonical JSON"):
        rebuild_from_bundle(broken, tmp_path / "destination.sqlite")


def test_rebuild_rejects_existing_destination_and_temp_collision(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    destination = tmp_path / "destination.sqlite"
    destination.write_text("preserve", encoding="utf-8")
    with pytest.raises(RecoveryError, match="already exists"):
        rebuild_from_bundle(bundle, destination)
    assert destination.read_text(encoding="utf-8") == "preserve"
    destination.unlink()
    temp = destination.with_name(f".{destination.name}.rebuild-{os.getpid()}")
    temp.write_text("collision", encoding="utf-8")
    with pytest.raises(RecoveryError, match="temporary path collision"):
        rebuild_from_bundle(bundle, destination)


def test_rebuild_operation_order_and_payload_validation(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, annotation=True)
    reversed_ops = _rehash({**bundle, "operations": list(reversed(bundle["operations"]))})
    with pytest.raises(RecoveryError, match="not revision ordered"):
        rebuild_from_bundle(reversed_ops, tmp_path / "reverse.sqlite")
    operations = [dict(item) for item in bundle["operations"]]
    operations[0]["payload"] = []
    broken = _rehash({**bundle, "operations": operations})
    with pytest.raises(RecoveryError, match="payload must be an object"):
        rebuild_from_bundle(broken, tmp_path / "payload.sqlite")


def test_rebuild_annotation_validation_branches(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, annotation=True)
    base_operations = [dict(item) for item in bundle["operations"]]

    def assert_broken(change, message, name):
        operations = [dict(item) for item in base_operations]
        annotation = dict(operations[1])
        payload = dict(annotation["payload"])
        change(annotation, payload)
        annotation["payload"] = payload
        operations[1] = annotation
        broken = _rehash({**bundle, "operations": operations})
        with pytest.raises(RecoveryError, match=message):
            rebuild_from_bundle(broken, tmp_path / f"{name}.sqlite")

    assert_broken(lambda op, p: p.pop("actor"), "document fields", "fields")
    assert_broken(
        lambda op, p: p.__setitem__("payload", []), "payload must be an object", "payload"
    )
    assert_broken(lambda op, p: p.__setitem__("actor", 1), "actor must be a string", "string")
    assert_broken(
        lambda op, p: p.__setitem__("new_revision", True),
        "new_revision must be an integer",
        "integer",
    )
    assert_broken(lambda op, p: p.__setitem__("entity_hash", "bad"), "hash is invalid", "hash")
    assert_broken(
        lambda op, p: op.__setitem__("entity_id", "annotation-wrong"),
        "identity mismatch",
        "identity",
    )


def test_rebuild_quarantine_validation_branches(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, quarantine=True)
    base_operations = [dict(item) for item in bundle["operations"]]

    def assert_broken(change, name):
        operations = [dict(item) for item in base_operations]
        quarantine = dict(operations[1])
        payload = dict(quarantine["payload"])
        change(quarantine, payload)
        quarantine["payload"] = payload
        operations[1] = quarantine
        broken = _rehash({**bundle, "operations": operations})
        with pytest.raises(RecoveryError, match="invalid rebuild quarantine operation"):
            rebuild_from_bundle(broken, tmp_path / f"q-{name}.sqlite")

    assert_broken(lambda op, p: p.pop("reason_code"), "fields")
    assert_broken(lambda op, p: p.__setitem__("payload_bytes_sha256", "0" * 64), "payload-checksum")
    assert_broken(lambda op, p: p.__setitem__("incoming_content_hash", "0" * 64), "incoming-hash")
    assert_broken(
        lambda op, p: p.__setitem__("quarantine_id", "quarantine-" + "0" * 48), "identity"
    )
    assert_broken(lambda op, p: p.__setitem__("reason_code", "OTHER"), "reason")
    assert_broken(lambda op, p: p.__setitem__("correlation_id", ""), "correlation")
    assert_broken(lambda op, p: p.__setitem__("revision", "bad"), "revision")
    assert_broken(lambda op, p: p.__setitem__("created_at_utc", "bad"), "timestamp")
    assert_broken(lambda op, p: p.__setitem__("existing_content_hash", "0" * 64), "existing")


def test_rebuild_audit_event_validation_branches(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    def assert_broken(change, name):
        events = [dict(item) for item in bundle["audit_events"]]
        change(events[0])
        broken = _rehash({**bundle, "audit_events": events})
        with pytest.raises(RecoveryError, match="invalid rebuild audit evidence"):
            rebuild_from_bundle(broken, tmp_path / f"audit-{name}.sqlite")

    assert_broken(lambda event: event.pop("actor"), "fields")
    assert_broken(lambda event: event.__setitem__("sequence", "bad"), "numeric")
    assert_broken(lambda event: event.__setitem__("sequence", 2), "sequence")
    assert_broken(lambda event: event.__setitem__("operation", "tampered"), "semantic")
    assert_broken(lambda event: event.__setitem__("timestamp_utc", "bad"), "timestamp")
    assert_broken(lambda event: event.__setitem__("timestamp_utc", "2026-01-01T00:00:00"), "naive")
    assert_broken(
        lambda event: event.__setitem__("timestamp_utc", "2026-01-01T00:00:00+00:00"),
        "noncanonical",
    )
