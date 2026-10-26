from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

import system_recovery.package as recovery_package
import system_recovery.snapshot as recovery_snapshot
from system_recovery import (
    RecoveryClass,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotRequest,
    SnapshotSpec,
    create_backup_package,
    load_backup_manifest,
    snapshot_entry,
)

_SOURCE_COMMIT = "c" * 40
_RELEASE_ID = "gap-004-exit-package"


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "private-host-root"
    backup = tmp_path / "backup-attempt"
    source.mkdir()
    backup.mkdir()
    return source, backup


def _write_sources(source: Path) -> None:
    (source / "core.json").write_text(
        '{"state":"raw-owner-payload-marker"}\n',
        encoding="utf-8",
    )
    database = source / "signal.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE factual_state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO factual_state(value) VALUES ('signal-state')")
    (source / "dataset.parquet").write_bytes(b"immutable-registered-source")


def _requests(source: Path) -> tuple[SnapshotRequest, ...]:
    return (
        SnapshotRequest(
            source_root=source,
            spec=SnapshotSpec(
                logical_id="signal-bot.owner-state",
                owner="signal-bot",
                recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                snapshot_kind=SnapshotKind.SQLITE,
                source="signal.sqlite3",
                restore_target="signal-bot/state.sqlite3",
            ),
        ),
        SnapshotRequest(
            source_root=source,
            spec=SnapshotSpec(
                logical_id="registered.dataset-source",
                owner="registered-dataset",
                recovery_class=RecoveryClass.RECONSTRUCTABLE_IMMUTABLE,
                snapshot_kind=SnapshotKind.REFERENCE,
                source="dataset.parquet",
                restore_target=None,
            ),
        ),
        SnapshotRequest(
            source_root=source,
            spec=SnapshotSpec(
                logical_id="core.paper-state",
                owner="core-paper",
                recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="core.json",
                restore_target="core/paper-state.json",
            ),
        ),
    )


def test_backup_package_is_sorted_closed_and_loads_by_trusted_digest(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)

    package = create_backup_package(
        backup_root=backup,
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        requests=_requests(source),
    )

    assert tuple(entry.logical_id for entry in package.manifest.entries) == (
        "core.paper-state",
        "registered.dataset-source",
        "signal-bot.owner-state",
    )
    assert package.manifest_path == backup / "recovery-manifest.json"
    assert package.manifest_path.read_bytes() == package.manifest.canonical_bytes()
    assert package.manifest_sha256 == hashlib.sha256(package.manifest.canonical_bytes()).hexdigest()
    assert {path.name for path in backup.iterdir()} == {"payload", "recovery-manifest.json"}
    assert {path.name for path in (backup / "payload").iterdir()} == {
        "core.paper-state",
        "signal-bot.owner-state",
    }
    assert not (backup / "payload" / "registered.dataset-source").exists()

    loaded = load_backup_manifest(
        backup_root=backup,
        expected_manifest_sha256=package.manifest_sha256,
    )
    assert loaded == package.manifest

    evidence = package.manifest.canonical_bytes()
    assert str(source).encode() not in evidence
    assert b"private-host-root" not in evidence
    assert b"raw-owner-payload-marker" not in evidence
    assert b"signal-state" not in evidence
    assert b"immutable-registered-source" not in evidence


def test_backup_manifest_digest_is_stable_for_same_canonical_package(tmp_path: Path) -> None:
    source, first_backup = _roots(tmp_path)
    second_backup = tmp_path / "second-backup-attempt"
    second_backup.mkdir()
    _write_sources(source)

    first = create_backup_package(
        backup_root=first_backup,
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        requests=_requests(source),
    )
    second = create_backup_package(
        backup_root=second_backup,
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        requests=tuple(reversed(_requests(source))),
    )

    assert first.manifest.canonical_bytes() == second.manifest.canonical_bytes()
    assert first.manifest_sha256 == second.manifest_sha256


@pytest.mark.parametrize("kind", ["wrong-digest", "malformed", "non-canonical"])
def test_backup_manifest_loader_fails_closed_on_untrusted_bytes(tmp_path: Path, kind: str) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    package = create_backup_package(
        backup_root=backup,
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        requests=_requests(source),
    )

    if kind == "wrong-digest":
        expected = "0" * 64
    elif kind == "malformed":
        raw = b"{not-json}\n"
        package.manifest_path.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()
    else:
        raw = (json.dumps(package.manifest.to_payload(), indent=2) + "\n").encode()
        package.manifest_path.write_bytes(raw)
        expected = hashlib.sha256(raw).hexdigest()

    with pytest.raises(RecoveryManifestError):
        load_backup_manifest(backup_root=backup, expected_manifest_sha256=expected)


def test_backup_preflight_rejects_duplicate_logical_id_before_mutation(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    duplicate = SnapshotRequest(
        source_root=source,
        spec=SnapshotSpec(
            logical_id="core.paper-state",
            owner="other-owner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="dataset.parquet",
            restore_target="other/state.bin",
        ),
    )

    with pytest.raises(RecoveryManifestError, match="logical_id"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=(_requests(source)[2], duplicate),
        )
    assert list(backup.iterdir()) == []


def test_backup_preflight_rejects_duplicate_restore_target_before_mutation(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    duplicate_target = SnapshotRequest(
        source_root=source,
        spec=SnapshotSpec(
            logical_id="other.owner-state",
            owner="other-owner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="dataset.parquet",
            restore_target="core/paper-state.json",
        ),
    )

    with pytest.raises(RecoveryManifestError, match="restore_target"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=(_requests(source)[2], duplicate_target),
        )
    assert list(backup.iterdir()) == []


def test_backup_preflight_rejects_missing_later_source_before_publication(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    missing = SnapshotRequest(
        source_root=source,
        spec=SnapshotSpec(
            logical_id="z-missing.state",
            owner="missing-owner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="missing.json",
            restore_target="missing/state.json",
        ),
    )

    with pytest.raises(RecoveryManifestError, match="does not exist"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=(_requests(source)[2], missing),
        )
    assert list(backup.iterdir()) == []


def test_backup_preflight_rejects_symlink_later_source_before_publication(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    link = source / "z-link.json"
    try:
        link.symlink_to("core.json")
    except OSError:
        pytest.skip("symlinks are unavailable")
    linked = SnapshotRequest(
        source_root=source,
        spec=SnapshotSpec(
            logical_id="z-linked.state",
            owner="linked-owner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="z-link.json",
            restore_target="linked/state.json",
        ),
    )

    with pytest.raises(RecoveryManifestError, match="symbolic links"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=(_requests(source)[2], linked),
        )
    assert list(backup.iterdir()) == []


def test_backup_late_snapshot_failure_cleans_current_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    real_snapshot = recovery_package.snapshot_entry
    calls = 0

    def fail_second_snapshot(
        *,
        source_root: Path,
        backup_root: Path,
        spec: SnapshotSpec,
    ) -> object:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RecoveryManifestError("injected later snapshot failure")
        return real_snapshot(source_root=source_root, backup_root=backup_root, spec=spec)

    monkeypatch.setattr(recovery_package, "snapshot_entry", fail_second_snapshot)
    with pytest.raises(RecoveryManifestError, match="injected later snapshot failure"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=(_requests(source)[2], _requests(source)[0]),
        )

    assert list(backup.iterdir()) == []


def test_backup_manifest_publication_failure_cleans_current_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)

    def fail_manifest(_root: Path, _data: bytes) -> Path:
        raise RecoveryManifestError("injected manifest publication failure")

    monkeypatch.setattr(recovery_package, "_publish_manifest", fail_manifest)
    with pytest.raises(RecoveryManifestError, match="injected manifest publication failure"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=(_requests(source)[2], _requests(source)[0]),
        )
    assert list(backup.iterdir()) == []


def test_backup_rejects_nonempty_attempt_root_without_touching_it(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    sentinel = backup / "foreign-owner-file"
    sentinel.write_text("do-not-touch", encoding="utf-8")

    with pytest.raises(RecoveryManifestError, match="must be empty"):
        create_backup_package(
            backup_root=backup,
            source_commit=_SOURCE_COMMIT,
            platform_release_id=_RELEASE_ID,
            requests=_requests(source),
        )
    assert sentinel.read_text(encoding="utf-8") == "do-not-touch"
    assert {path.name for path in backup.iterdir()} == {sentinel.name}


def test_snapshot_publication_race_does_not_overwrite_foreign_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    real_link = recovery_snapshot.os.link
    target = backup / "payload" / "core.paper-state"

    def race_link(src: Path, dst: Path, *, follow_symlinks: bool = True) -> None:
        Path(dst).write_text("foreign-publication", encoding="utf-8")
        real_link(src, dst, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(recovery_snapshot.os, "link", race_link)
    with pytest.raises(RecoveryManifestError, match="appeared before publication"):
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=_requests(source)[2].spec,
        )
    assert target.read_text(encoding="utf-8") == "foreign-publication"


def test_snapshot_post_publication_fsync_failure_rolls_back_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    real_fsync = recovery_snapshot._fsync_directory
    calls = 0

    def fail_first_fsync(path: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected fsync failure")
        real_fsync(path)

    monkeypatch.setattr(recovery_snapshot, "_fsync_directory", fail_first_fsync)
    with pytest.raises(RecoveryManifestError):
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=_requests(source)[2].spec,
        )
    assert not (backup / "payload" / "core.paper-state").exists()


def test_snapshot_rejects_symlinked_backup_payload_parent(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    _write_sources(source)
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (backup / "payload").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RecoveryManifestError, match="real directory"):
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=_requests(source)[2].spec,
        )
    assert list(outside.iterdir()) == []


def test_secret_snapshot_spec_fails_before_package_mutation(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    secret = source / "credential.txt"
    secret.write_text("raw-credential-marker", encoding="utf-8")

    with pytest.raises(RecoveryManifestError, match="must not be copied"):
        SnapshotSpec(
            logical_id="secret.credentials",
            owner="operator-secrets",
            recovery_class=RecoveryClass.SECRET_PRIVATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="credential.txt",
            restore_target="secrets/credential.txt",
        )
    assert list(backup.iterdir()) == []
