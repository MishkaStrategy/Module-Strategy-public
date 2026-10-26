from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import system_recovery.composition as recovery_composition
import system_recovery.restore as recovery_restore
from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotSpec,
    restore_manifest_entries,
    snapshot_entry,
)

_SOURCE_COMMIT = "a" * 40
_RELEASE_ID = "platform-clean-host-restore"


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    clean = tmp_path / "clean"
    source.mkdir()
    backup.mkdir()
    clean.mkdir()
    return source, backup, clean


def _write_sqlite(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE factual_state (value TEXT NOT NULL)")
        connection.execute("INSERT INTO factual_state(value) VALUES (?)", (value,))


def _read_sqlite(path: Path) -> str:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        row = connection.execute("SELECT value FROM factual_state").fetchone()
    assert row is not None
    return str(row[0])


def _regular_manifest(source: Path, backup: Path) -> RecoveryManifest:
    first = source / "owners" / "alpha.state"
    second = source / "owners" / "omega.state"
    first.parent.mkdir(parents=True, exist_ok=True)
    first.write_text("alpha\n", encoding="utf-8")
    second.write_text("omega\n", encoding="utf-8")
    entries = (
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=SnapshotSpec(
                logical_id="alpha.owner-state",
                owner="alpha-owner",
                recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="owners/alpha.state",
                restore_target="owners/alpha.state",
            ),
        ),
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=SnapshotSpec(
                logical_id="omega.owner-state",
                owner="omega-owner",
                recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="owners/omega.state",
                restore_target="owners/omega.state",
            ),
        ),
    )
    return RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=entries,
    )


def test_platform_manifest_restores_multiple_owners_and_keeps_references_external(
    tmp_path: Path,
) -> None:
    source, backup, clean = _roots(tmp_path)
    _write_sqlite(source / "owners" / "core.sqlite3", "core-factual-state")
    _write_sqlite(source / "owners" / "signal.sqlite3", "signal-factual-state")
    registered = source / "registered" / "dataset.parquet"
    registered.parent.mkdir(parents=True)
    registered.write_bytes(b"immutable-registered-source")
    evidence = source / "evidence" / "acceptance.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_text('{"status":"PASS"}\n', encoding="utf-8")

    entries = tuple(
        sorted(
            (
                snapshot_entry(
                    source_root=source,
                    backup_root=backup,
                    spec=SnapshotSpec(
                        logical_id="core.paper-state",
                        owner="core-paper",
                        recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                        snapshot_kind=SnapshotKind.SQLITE,
                        source="owners/core.sqlite3",
                        restore_target="owners/core.sqlite3",
                    ),
                ),
                snapshot_entry(
                    source_root=source,
                    backup_root=backup,
                    spec=SnapshotSpec(
                        logical_id="registered.dataset-source",
                        owner="registered-dataset",
                        recovery_class=RecoveryClass.RECONSTRUCTABLE_IMMUTABLE,
                        snapshot_kind=SnapshotKind.REFERENCE,
                        source="registered/dataset.parquet",
                        restore_target=None,
                    ),
                ),
                snapshot_entry(
                    source_root=source,
                    backup_root=backup,
                    spec=SnapshotSpec(
                        logical_id="signal-bot.owner-state",
                        owner="signal-bot",
                        recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                        snapshot_kind=SnapshotKind.SQLITE,
                        source="owners/signal.sqlite3",
                        restore_target="owners/signal.sqlite3",
                    ),
                ),
                snapshot_entry(
                    source_root=source,
                    backup_root=backup,
                    spec=SnapshotSpec(
                        logical_id="system-acceptance.evidence",
                        owner="system-acceptance",
                        recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
                        snapshot_kind=SnapshotKind.REGULAR_FILE,
                        source="evidence/acceptance.json",
                        restore_target="evidence/acceptance.json",
                    ),
                ),
            ),
            key=lambda entry: entry.logical_id,
        )
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=entries,
    )

    restored = restore_manifest_entries(
        backup_root=backup,
        restore_root=clean,
        manifest=manifest,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )
    assert restored == (
        clean / "owners" / "core.sqlite3",
        clean / "owners" / "signal.sqlite3",
        clean / "evidence" / "acceptance.json",
    )
    assert _read_sqlite(restored[0]) == "core-factual-state"
    assert _read_sqlite(restored[1]) == "signal-factual-state"
    assert restored[2].read_text(encoding="utf-8") == '{"status":"PASS"}\n'
    assert not (clean / "registered" / "dataset.parquet").exists()
    assert not (backup / "payload" / "registered.dataset-source").exists()

    repeated = restore_manifest_entries(
        backup_root=backup,
        restore_root=clean,
        manifest=manifest,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )
    assert repeated == restored
    assert _read_sqlite(restored[0]) == "core-factual-state"
    assert _read_sqlite(restored[1]) == "signal-factual-state"


@pytest.mark.parametrize("damage", ["corrupt", "missing"])
def test_platform_preflight_rejects_later_bad_payload_before_any_publication(
    tmp_path: Path,
    damage: str,
) -> None:
    source, backup, clean = _roots(tmp_path)
    manifest = _regular_manifest(source, backup)
    later_payload = backup / "payload" / "omega.owner-state"
    if damage == "corrupt":
        later_payload.write_text("corrupt\n", encoding="utf-8")
    else:
        later_payload.unlink()

    with pytest.raises(RecoveryManifestError, match="snapshot"):
        restore_manifest_entries(
            backup_root=backup,
            restore_root=clean,
            manifest=manifest,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )
    assert not (clean / "owners" / "alpha.state").exists()
    assert not (clean / "owners" / "omega.state").exists()


@pytest.mark.parametrize("identity", ["source", "release"])
def test_platform_wrong_identity_fails_before_publication(tmp_path: Path, identity: str) -> None:
    source, backup, clean = _roots(tmp_path)
    manifest = _regular_manifest(source, backup)
    expected_source = "b" * 40 if identity == "source" else _SOURCE_COMMIT
    expected_release = "wrong-release" if identity == "release" else _RELEASE_ID

    with pytest.raises(RecoveryManifestError, match="mismatch"):
        restore_manifest_entries(
            backup_root=backup,
            restore_root=clean,
            manifest=manifest,
            expected_source_commit=expected_source,
            expected_platform_release_id=expected_release,
        )
    assert not (clean / "owners" / "alpha.state").exists()
    assert not (clean / "owners" / "omega.state").exists()


def test_platform_partial_restore_root_is_rejected(tmp_path: Path) -> None:
    source, backup, clean = _roots(tmp_path)
    manifest = _regular_manifest(source, backup)
    restored = restore_manifest_entries(
        backup_root=backup,
        restore_root=clean,
        manifest=manifest,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )
    restored[1].unlink()

    with pytest.raises(RecoveryManifestError, match="partial recovery publication"):
        restore_manifest_entries(
            backup_root=backup,
            restore_root=clean,
            manifest=manifest,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )
    assert restored[0].read_text(encoding="utf-8") == "alpha\n"
    assert not restored[1].exists()


def test_platform_late_restore_failure_rolls_back_current_publications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup, clean = _roots(tmp_path)
    manifest = _regular_manifest(source, backup)
    real_restore = recovery_composition._restore_snapshot_entry_with_publication
    call_count = 0

    def fail_second_restore(
        *,
        backup_root: Path,
        restore_root: Path,
        manifest: RecoveryManifest,
        logical_id: str,
        expected_source_commit: str,
        expected_platform_release_id: str,
    ) -> tuple[Path, bool]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RecoveryManifestError("injected late restore failure")
        return real_restore(
            backup_root=backup_root,
            restore_root=restore_root,
            manifest=manifest,
            logical_id=logical_id,
            expected_source_commit=expected_source_commit,
            expected_platform_release_id=expected_platform_release_id,
        )

    monkeypatch.setattr(
        recovery_composition,
        "_restore_snapshot_entry_with_publication",
        fail_second_restore,
    )
    with pytest.raises(RecoveryManifestError, match="injected late restore failure"):
        restore_manifest_entries(
            backup_root=backup,
            restore_root=clean,
            manifest=manifest,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )

    assert not (clean / "owners" / "alpha.state").exists()
    assert not (clean / "owners" / "omega.state").exists()


def test_platform_rollback_keeps_matching_target_published_by_other_actor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup, clean = _roots(tmp_path)
    manifest = _regular_manifest(source, backup)
    real_prepare = recovery_restore._prepare_destination
    injected = False

    def prepare_with_matching_race(root: Path, relative: str) -> Path:
        nonlocal injected
        target = real_prepare(root, relative)
        if relative == "owners/alpha.state" and not injected:
            target.write_text("alpha\n", encoding="utf-8")
            injected = True
        return target

    monkeypatch.setattr(recovery_restore, "_prepare_destination", prepare_with_matching_race)
    real_restore = recovery_composition._restore_snapshot_entry_with_publication
    call_count = 0

    def fail_second_restore(
        *,
        backup_root: Path,
        restore_root: Path,
        manifest: RecoveryManifest,
        logical_id: str,
        expected_source_commit: str,
        expected_platform_release_id: str,
    ) -> tuple[Path, bool]:
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise RecoveryManifestError("injected late restore failure")
        return real_restore(
            backup_root=backup_root,
            restore_root=restore_root,
            manifest=manifest,
            logical_id=logical_id,
            expected_source_commit=expected_source_commit,
            expected_platform_release_id=expected_platform_release_id,
        )

    monkeypatch.setattr(
        recovery_composition,
        "_restore_snapshot_entry_with_publication",
        fail_second_restore,
    )
    with pytest.raises(RecoveryManifestError, match="injected late restore failure"):
        restore_manifest_entries(
            backup_root=backup,
            restore_root=clean,
            manifest=manifest,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )

    raced_target = clean / "owners" / "alpha.state"
    assert raced_target.read_text(encoding="utf-8") == "alpha\n"
    assert not (clean / "owners" / "omega.state").exists()
