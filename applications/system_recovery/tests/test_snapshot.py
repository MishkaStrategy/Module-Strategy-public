from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

from system_recovery import (
    RecoveryClass,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotSpec,
    snapshot_entry,
    verify_snapshot_file,
)


def _roots(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "state"
    backup = tmp_path / "backup"
    source.mkdir()
    backup.mkdir()
    return source, backup


def test_regular_file_snapshot_is_checksummed_and_verifiable(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    state = source / "runner.json"
    state.write_text('{"state":"paused"}\n', encoding="utf-8")

    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="runner.state",
            owner="trading-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="runner.json",
            restore_target="runner/state.json",
        ),
    )

    verify_snapshot_file(backup, entry)
    assert (backup / "payload" / "runner.state").read_bytes() == state.read_bytes()
    assert entry.byte_size == len(state.read_bytes())


def test_sqlite_snapshot_uses_consistent_backup_api(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    database = source / "paper.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE fills(id INTEGER PRIMARY KEY, value TEXT NOT NULL)")
        connection.execute("INSERT INTO fills(value) VALUES ('one')")
        connection.commit()

    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="paper.db",
            owner="paper-execution",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source="paper.sqlite3",
            restore_target="paper/state.sqlite3",
        ),
    )

    verify_snapshot_file(backup, entry)
    with sqlite3.connect(backup / "payload" / "paper.db") as connection:
        assert connection.execute("SELECT value FROM fills").fetchall() == [("one",)]


def test_sqlite_snapshot_handles_uri_metacharacters_in_filename(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    database = source / "paper?state#1.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE state(value TEXT NOT NULL)")
        connection.execute("INSERT INTO state(value) VALUES ('ok')")
        connection.commit()

    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="paper.uri-db",
            owner="paper-execution",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source="paper?state#1.sqlite3",
            restore_target="paper/state.sqlite3",
        ),
    )

    verify_snapshot_file(backup, entry)
    with sqlite3.connect(backup / "payload" / "paper.uri-db") as connection:
        assert connection.execute("SELECT value FROM state").fetchall() == [("ok",)]


def test_snapshot_rejects_symlink_source(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    real = source / "real.json"
    real.write_text("{}\n", encoding="utf-8")
    link = source / "state.json"
    try:
        link.symlink_to(real.name)
    except OSError:
        pytest.skip("symlinks are unavailable")

    with pytest.raises(RecoveryManifestError, match="symbolic links"):
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=SnapshotSpec(
                logical_id="runner.state",
                owner="trading-runner",
                recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="state.json",
                restore_target="runner/state.json",
            ),
        )


def test_snapshot_rejects_special_file_and_existing_destination(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    fifo = source / "pipe"
    if hasattr(os, "mkfifo"):
        os.mkfifo(fifo)
        with pytest.raises(RecoveryManifestError, match="regular file"):
            snapshot_entry(
                source_root=source,
                backup_root=backup,
                spec=SnapshotSpec(
                    logical_id="runtime.pipe",
                    owner="trading-runner",
                    recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                    snapshot_kind=SnapshotKind.REGULAR_FILE,
                    source="pipe",
                    restore_target="runtime/pipe",
                ),
            )

    state = source / "runner.json"
    state.write_text("{}\n", encoding="utf-8")
    (backup / "payload").mkdir()
    (backup / "payload" / "runner.state").write_text("existing", encoding="utf-8")
    with pytest.raises(RecoveryManifestError, match="already exists"):
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=SnapshotSpec(
                logical_id="runner.state",
                owner="trading-runner",
                recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="runner.json",
                restore_target="runner/state.json",
            ),
        )


def test_snapshot_refuses_to_copy_secret_or_cache(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    state = source / "secret.txt"
    state.write_text("secret", encoding="utf-8")

    for recovery_class in (RecoveryClass.SECRET_PRIVATE, RecoveryClass.EPHEMERAL_CACHE):
        with pytest.raises(RecoveryManifestError, match="must not be copied"):
            SnapshotSpec(
                logical_id=f"blocked.{recovery_class.value}",
                owner="system-recovery",
                recovery_class=recovery_class,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="secret.txt",
                restore_target="blocked/secret.txt",
            )


def test_invalid_spec_fails_before_backup_mutation(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    (source / "runner.json").write_text("{}\n", encoding="utf-8")

    with pytest.raises(RecoveryManifestError, match="restore_target"):
        SnapshotSpec(
            logical_id="runner.state",
            owner="trading-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="runner.json",
            restore_target="../escape.json",
        )

    assert list(backup.iterdir()) == []
