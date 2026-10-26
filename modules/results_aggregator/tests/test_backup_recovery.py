from __future__ import annotations

from pathlib import Path

import pytest

from modular_results_registry.canonical import canonical_sha256
from modular_results_registry.errors import RecoveryError
from modular_results_registry.recovery import create_backup, rebuild_from_bundle, restore_backup
from modular_results_registry.storage import SQLiteRegistry
from conftest import make_package


def test_backup_restore_and_rebuild_match_state(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest = tmp_path / "backup.json"
    restored_db = tmp_path / "restored.sqlite"
    rebuilt_db = tmp_path / "rebuilt.sqlite"
    with SQLiteRegistry(source_db) as source:
        source.register_package(make_package())
        expected_verified = source.verified_state_hash()
        expected_entity = source.entity_state_hash()
        backup = create_backup(source, backup_db, manifest)
        bundle = source.export_rebuild_bundle()
    assert backup.entity_state_hash == expected_entity
    restore_backup(backup_db, manifest, restored_db)
    rebuild_from_bundle(bundle, rebuilt_db)
    with SQLiteRegistry(restored_db) as restored, SQLiteRegistry(rebuilt_db) as rebuilt:
        assert restored.verified_state_hash() == expected_verified
        assert restored.entity_state_hash() == expected_entity
        assert rebuilt.verified_state_hash() == expected_verified
        assert rebuilt.entity_state_hash() == expected_entity
        assert restored.verify_integrity()["sqlite_integrity"] == "ok"
        assert rebuilt.verify_integrity()["sqlite_integrity"] == "ok"


def test_backup_manifest_and_database_corruption_are_blocked(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest = tmp_path / "backup.json"
    with SQLiteRegistry(source_db) as source:
        source.register_package(make_package())
        create_backup(source, backup_db, manifest)
    manifest.write_text("{}", encoding="utf-8")
    with pytest.raises(RecoveryError):
        restore_backup(backup_db, manifest, tmp_path / "restored.sqlite")

    with SQLiteRegistry(source_db) as source:
        manifest.unlink()
        backup_db.unlink()
        create_backup(source, backup_db, manifest)
    backup_db.write_bytes(backup_db.read_bytes() + b"tamper")
    with pytest.raises(RecoveryError, match="checksum mismatch"):
        restore_backup(backup_db, manifest, tmp_path / "restored-2.sqlite")


def test_rebuild_bundle_hash_and_destination_no_clobber(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    with SQLiteRegistry(source_db) as source:
        source.register_package(make_package())
        bundle = source.export_rebuild_bundle()
    broken = dict(bundle)
    broken["expected_verified_state_hash"] = "0" * 64
    with pytest.raises(RecoveryError, match="bundle hash mismatch"):
        rebuild_from_bundle(broken, tmp_path / "rebuilt.sqlite")

    destination = tmp_path / "exists.sqlite"
    destination.write_text("do not overwrite", encoding="utf-8")
    with pytest.raises(RecoveryError, match="already exists"):
        rebuild_from_bundle(bundle, destination)
    assert destination.read_text(encoding="utf-8") == "do not overwrite"


def test_backup_is_idempotently_refused_when_targets_exist(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    backup_db = tmp_path / "backup.sqlite"
    manifest = tmp_path / "backup.json"
    with SQLiteRegistry(source_db) as source:
        source.register_package(make_package())
        create_backup(source, backup_db, manifest)
        with pytest.raises(RecoveryError, match="already exists"):
            create_backup(source, backup_db, manifest)


def test_rebuild_replays_annotations_and_exact_state_hash(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    rebuilt_db = tmp_path / "rebuilt.sqlite"
    with SQLiteRegistry(source_db) as source:
        package = make_package()
        source.register_package(package)
        source.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="auditor",
            reason="LIMITATION",
            payload={"text": "synthetic fixture"},
        )
        expected_revision = source.revision()
        expected_verified = source.verified_state_hash()
        expected_entity = source.entity_state_hash()
        bundle = source.export_rebuild_bundle()
    assert len(bundle["operations"]) == 2
    assert [item["operation_type"] for item in bundle["operations"]] == [
        "package",
        "annotation",
    ]
    rebuild_from_bundle(bundle, rebuilt_db)
    with SQLiteRegistry(rebuilt_db) as rebuilt:
        assert rebuilt.revision() == expected_revision
        assert rebuilt.verified_state_hash() == expected_verified
        assert rebuilt.entity_state_hash() == expected_entity
        assert rebuilt.annotations(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
        )[0]["payload"] == {"text": "synthetic fixture"}


def test_rebuild_annotation_tampering_is_blocked(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite"
    with SQLiteRegistry(source_db) as source:
        package = make_package()
        source.register_package(package)
        source.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="auditor",
            reason="LIMITATION",
            payload={"text": "synthetic fixture"},
        )
        bundle = source.export_rebuild_bundle()
    operations = [dict(item) for item in bundle["operations"]]
    annotation = dict(operations[1])
    annotation_payload = dict(annotation["payload"])
    annotation_payload["actor"] = "attacker"
    annotation["payload"] = annotation_payload
    operations[1] = annotation
    broken = dict(bundle)
    broken["operations"] = operations
    broken["bundle_hash"] = canonical_sha256(
        {key: value for key, value in broken.items() if key != "bundle_hash"}
    )
    with pytest.raises(RecoveryError):
        rebuild_from_bundle(broken, tmp_path / "rebuilt.sqlite")
