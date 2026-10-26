from __future__ import annotations

from pathlib import Path

import pytest

import system_recovery.restore as restore_module
from system_recovery import (
    RecoveryClass,
    RecoveryEntry,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_SOURCE_COMMIT = "2" * 40
_RELEASE_ID = "full-rc-restore-test"


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    restore = tmp_path / "restore"
    source.mkdir()
    backup.mkdir()
    restore.mkdir()
    return source, backup, restore


def _regular_entry(source: Path, backup: Path) -> RecoveryEntry:
    state = source / "config" / "settings.json"
    state.parent.mkdir(parents=True)
    state.write_bytes(b'{"mode":"paper"}\n')
    return snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="required.settings",
            owner="system-recovery-test",
            recovery_class=RecoveryClass.REQUIRED_CONFIGURATION,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="config/settings.json",
            restore_target="config/settings.json",
        ),
    )


def _manifest(*entries: RecoveryEntry) -> RecoveryManifest:
    return RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=tuple(sorted(entries, key=lambda item: item.logical_id)),
    )


def _restore(
    *,
    backup: Path,
    restore: Path,
    manifest: RecoveryManifest,
    logical_id: str,
) -> Path:
    return restore_snapshot_entry(
        backup_root=backup,
        restore_root=restore,
        manifest=manifest,
        logical_id=logical_id,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )


def test_restore_is_exact_idempotent_and_never_overwrites_mismatch(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)

    target = _restore(
        backup=backup,
        restore=restore,
        manifest=manifest,
        logical_id=entry.logical_id,
    )
    assert target.read_bytes() == b'{"mode":"paper"}\n'
    assert (
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )
        == target
    )

    target.write_bytes(b"different\n")
    with pytest.raises(RecoveryManifestError, match="different identity"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )
    assert target.read_bytes() == b"different\n"
    assert list(target.parent.glob(f".{target.name}.*.restore")) == []


def test_restore_rejects_manifest_source_or_release_mismatch(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)

    with pytest.raises(RecoveryManifestError, match="source commit mismatch"):
        restore_snapshot_entry(
            backup_root=backup,
            restore_root=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
            expected_source_commit="3" * 40,
            expected_platform_release_id=_RELEASE_ID,
        )
    with pytest.raises(RecoveryManifestError, match="platform release mismatch"):
        restore_snapshot_entry(
            backup_root=backup,
            restore_root=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id="different-release",
        )
    assert not (restore / "config" / "settings.json").exists()


def test_restore_rejects_missing_logical_id_before_publication(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    manifest = _manifest(_regular_entry(source, backup))

    with pytest.raises(RecoveryManifestError, match="logical_id"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id="missing.entry",
        )
    assert not (restore / "config" / "settings.json").exists()


def test_restore_rejects_corrupt_backup_before_publication(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)
    (backup / "payload" / entry.logical_id).write_bytes(b"corrupt\n")

    with pytest.raises(RecoveryManifestError, match="snapshot"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )
    assert not (restore / "config" / "settings.json").exists()


def test_restore_rejects_symlinked_parent(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (restore / "config").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RecoveryManifestError, match="parent"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )
    assert list(outside.iterdir()) == []


def test_restore_rejects_symlinked_target(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)
    target_parent = restore / "config"
    target_parent.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_bytes(b"outside\n")
    try:
        (target_parent / "settings.json").symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RecoveryManifestError, match="symbolic link"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )
    assert outside.read_bytes() == b"outside\n"


def test_post_publication_fsync_failure_rolls_back_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)
    target = restore / "config" / "settings.json"
    original_fsync = restore_module._fsync_directory
    calls = 0

    def fail_first(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected publication fsync failure")
        original_fsync(path)

    monkeypatch.setattr(restore_module, "_fsync_directory", fail_first)

    with pytest.raises(RecoveryManifestError, match="snapshot restore failed safely"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )

    assert calls == 2
    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*.restore")) == []


def test_post_publication_rollback_durability_failure_is_not_hidden(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup, restore = _roots(tmp_path)
    entry = _regular_entry(source, backup)
    manifest = _manifest(entry)
    target = restore / "config" / "settings.json"

    def fail_fsync(_path: Path) -> None:
        raise OSError("injected fsync failure")

    monkeypatch.setattr(restore_module, "_fsync_directory", fail_fsync)

    with pytest.raises(RecoveryManifestError, match="rollback durability could not be verified"):
        _restore(
            backup=backup,
            restore=restore,
            manifest=manifest,
            logical_id=entry.logical_id,
        )

    assert not target.exists()
    assert list(target.parent.glob(f".{target.name}.*.restore")) == []


def test_protected_immutable_evidence_is_embeddable_and_restorable(tmp_path: Path) -> None:
    source, backup, restore = _roots(tmp_path)
    evidence = source / "evidence" / "result.json"
    evidence.parent.mkdir(parents=True)
    evidence.write_bytes(b'{"digest":"accepted"}\n')
    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="research.result",
            owner="research-runner",
            recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="evidence/result.json",
            restore_target="evidence/result.json",
        ),
    )

    restored = _restore(
        backup=backup,
        restore=restore,
        manifest=_manifest(entry),
        logical_id=entry.logical_id,
    )
    assert restored.read_bytes() == evidence.read_bytes()
