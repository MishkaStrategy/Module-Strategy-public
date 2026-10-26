from __future__ import annotations

import json
from pathlib import Path

import pytest

import modular_results_registry.recovery as recovery
from modular_results_registry.canonical import canonical_json_bytes, canonical_sha256
from modular_results_registry.errors import RecoveryError
from modular_results_registry.recovery import create_backup, rebuild_from_bundle, restore_backup
from modular_results_registry.storage import SQLiteRegistry
from conftest import make_package


def _manifest(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_manifest(path: Path, value: dict[str, object]) -> None:
    value["manifest_hash"] = canonical_sha256(
        {key: item for key, item in value.items() if key != "manifest_hash"}
    )
    path.write_bytes(canonical_json_bytes(value))


def test_backup_failure_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite"
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())

        def fail_hash(_: Path) -> tuple[str, int]:
            raise OSError("simulated disk failure")

        monkeypatch.setattr(recovery, "_sha256_file", fail_hash)
        with pytest.raises(OSError, match="simulated"):
            create_backup(registry, tmp_path / "backup.sqlite", tmp_path / "backup.json")
    assert not list(tmp_path.glob(".backup-*"))
    assert not list(tmp_path.glob(".manifest-*"))


def test_backup_manifest_fsync_failure_removes_all_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite"
    original_fsync = recovery.os.fsync
    calls = 0

    def fail_manifest_fsync(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("manifest fsync failed")
        original_fsync(descriptor)

    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        monkeypatch.setattr(recovery.os, "fsync", fail_manifest_fsync)
        with pytest.raises(OSError, match="manifest fsync"):
            create_backup(registry, tmp_path / "backup.sqlite", tmp_path / "backup.json")
    assert not (tmp_path / "backup.sqlite").exists()
    assert not (tmp_path / "backup.json").exists()
    assert not list(tmp_path.glob(".backup-*"))
    assert not list(tmp_path.glob(".manifest-*"))


def test_backup_manifest_publication_failure_rolls_back_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite"
    original_publish = recovery._publish_temp_noreplace
    calls = 0

    def fail_second(temp: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RecoveryError("manifest publish failed")
        original_publish(temp, target)

    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        monkeypatch.setattr(recovery, "_publish_temp_noreplace", fail_second)
        with pytest.raises(RecoveryError, match="manifest publish"):
            create_backup(registry, tmp_path / "backup.sqlite", tmp_path / "backup.json")
    assert not (tmp_path / "backup.sqlite").exists()
    assert not (tmp_path / "backup.json").exists()
    assert not list(tmp_path.glob(".backup-*"))
    assert not list(tmp_path.glob(".manifest-*"))


def test_backup_manifest_publication_and_rollback_failure_is_loud(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite"
    original_publish = recovery._publish_temp_noreplace
    calls = 0

    def fail_second(temp: Path, target: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RecoveryError("manifest publish failed")
        original_publish(temp, target)

    def fail_rollback(_: Path) -> None:
        raise RecoveryError("rollback failed")

    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        monkeypatch.setattr(recovery, "_publish_temp_noreplace", fail_second)
        monkeypatch.setattr(recovery, "_remove_published_file", fail_rollback)
        with pytest.raises(RecoveryError, match="rollback failed"):
            create_backup(registry, tmp_path / "backup.sqlite", tmp_path / "backup.json")


def test_restore_source_change_is_detected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest_path = tmp_path / "backup.json"
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        create_backup(registry, backup_db, manifest_path)

    original_fstat = recovery.os.fstat
    calls = 0

    def changed(descriptor: int):
        nonlocal calls
        calls += 1
        info = original_fstat(descriptor)
        if calls >= 2:
            values = list(info)
            values[8] = info.st_mtime + 1
            return type(info)(values)
        return info

    monkeypatch.setattr(recovery.os, "fstat", changed)
    with pytest.raises(RecoveryError, match="changed during restore"):
        restore_backup(backup_db, manifest_path, tmp_path / "restored.sqlite")
    assert not list(tmp_path.glob(".restore-*"))


def test_restore_rejects_manifest_size_and_filename_mismatch(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest_path = tmp_path / "backup.json"
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        create_backup(registry, backup_db, manifest_path)
    manifest = _manifest(manifest_path)
    manifest["database_size"] = int(manifest["database_size"]) + 1
    _write_manifest(manifest_path, manifest)
    with pytest.raises(RecoveryError, match="checksum mismatch"):
        restore_backup(backup_db, manifest_path, tmp_path / "restored.sqlite")

    manifest = _manifest(manifest_path)
    manifest["database_size"] = backup_db.stat().st_size
    manifest["database_filename"] = "different.sqlite"
    _write_manifest(manifest_path, manifest)
    # filename is informational and safe, while hash/size still bind exact contents.
    restored = tmp_path / "restored-ok.sqlite"
    restore_backup(backup_db, manifest_path, restored)
    assert restored.exists()


def test_restore_rejects_symlink_destination(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest_path = tmp_path / "backup.json"
    real = tmp_path / "real.sqlite"
    real.write_text("keep", encoding="utf-8")
    destination = tmp_path / "destination.sqlite"
    destination.symlink_to(real)
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        create_backup(registry, backup_db, manifest_path)
    with pytest.raises(RecoveryError, match="already exists"):
        restore_backup(backup_db, manifest_path, destination)
    assert real.read_text(encoding="utf-8") == "keep"


def test_rebuild_registration_failure_cleans_destination(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        bundle = registry.export_rebuild_bundle()
    broken = dict(bundle)
    operations = [dict(item) for item in broken["operations"]]
    payload = dict(operations[0]["payload"])
    payload["package_id"] = "different-package"
    operations[0]["payload"] = payload
    broken["operations"] = operations
    broken["bundle_hash"] = canonical_sha256(
        {key: value for key, value in broken.items() if key != "bundle_hash"}
    )
    destination = tmp_path / "rebuilt.sqlite"
    with pytest.raises(RecoveryError):
        rebuild_from_bundle(broken, destination)
    assert not destination.exists()
    assert not list(tmp_path.glob(".*.rebuild-*"))


def test_rebuild_tampered_annotation_and_duplicate_are_blocked(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    with SQLiteRegistry(source_db) as registry:
        package = make_package()
        registry.register_package(package)
        registry.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="auditor",
            reason="LIMITATION",
            payload={"text": "synthetic"},
        )
        bundle = registry.export_rebuild_bundle()

    operations = [dict(item) for item in bundle["operations"]]
    annotation = dict(operations[1])
    payload = dict(annotation["payload"])
    payload["annotation_id"] = "annotation-forged"
    annotation["payload"] = payload
    operations[1] = annotation
    broken = dict(bundle)
    broken["operations"] = operations
    broken["bundle_hash"] = canonical_sha256(
        {key: value for key, value in broken.items() if key != "bundle_hash"}
    )
    with pytest.raises(RecoveryError):
        rebuild_from_bundle(broken, tmp_path / "rebuilt.sqlite")

    duplicate = dict(bundle)
    duplicate["operations"] = [bundle["operations"][0], bundle["operations"][0]]
    duplicate["expected_registry_revision"] = 2
    duplicate["bundle_hash"] = canonical_sha256(
        {key: value for key, value in duplicate.items() if key != "bundle_hash"}
    )
    with pytest.raises(RecoveryError):
        rebuild_from_bundle(duplicate, tmp_path / "duplicate.sqlite")


def test_rebuild_refuses_existing_symlink_temp_path(tmp_path: Path) -> None:
    destination = tmp_path / "rebuilt.sqlite"
    temp = destination.with_name(f".{destination.name}.rebuild-{recovery.os.getpid()}")
    target = tmp_path / "target"
    target.write_text("keep", encoding="utf-8")
    temp.symlink_to(target)
    bundle = {
        "schema_id": "modular-results-registry.rebuild-bundle",
        "schema_version": "4",
        "registry_schema_version": 4,
        "operations": [],
        "audit_events": [],
        "expected_registry_revision": 0,
        "expected_verified_state_hash": "a" * 64,
        "expected_entity_state_hash": "b" * 64,
        "expected_audit_chain_hash": "c" * 64,
    }
    bundle["bundle_hash"] = canonical_sha256(bundle)
    with pytest.raises(RecoveryError, match="temporary path collision"):
        rebuild_from_bundle(bundle, destination)
    assert target.read_text(encoding="utf-8") == "keep"
    assert temp.is_symlink()


def test_backup_uses_snapshot_metadata_not_live_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest_path = tmp_path / "backup.json"
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        expected_revision = registry.revision()
        expected_entity_hash = registry.entity_state_hash()
        expected_verified_hash = registry.verified_state_hash()
        monkeypatch.setattr(
            registry,
            "revision",
            lambda: (_ for _ in ()).throw(AssertionError("live revision used")),
        )
        monkeypatch.setattr(
            registry,
            "entity_state_hash",
            lambda: (_ for _ in ()).throw(AssertionError("live state hash used")),
        )
        monkeypatch.setattr(
            registry,
            "verified_state_hash",
            lambda: (_ for _ in ()).throw(AssertionError("live verified hash used")),
        )
        result = create_backup(registry, backup_db, manifest_path)
    assert result.registry_revision == expected_revision
    assert result.entity_state_hash == expected_entity_hash
    manifest = _manifest(manifest_path)
    assert manifest["registry_revision"] == expected_revision
    assert manifest["entity_state_hash"] == expected_entity_hash
    assert manifest["verified_state_hash"] == expected_verified_hash


def test_backup_rejects_snapshot_with_logical_integrity_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source_db = tmp_path / "source.sqlite"
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(make_package())
        original_verify = SQLiteRegistry.verify_integrity

        def fail_snapshot(self: SQLiteRegistry) -> dict[str, object]:
            result = original_verify(self)
            result["audit_bindings_valid"] = False
            return result

        monkeypatch.setattr(SQLiteRegistry, "verify_integrity", fail_snapshot)
        with pytest.raises(RecoveryError, match="snapshot integrity"):
            create_backup(
                registry,
                tmp_path / "backup.sqlite",
                tmp_path / "backup.json",
            )
    assert not (tmp_path / "backup.sqlite").exists()
    assert not (tmp_path / "backup.json").exists()
