from __future__ import annotations

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


def _write_source_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE fills(value TEXT NOT NULL)")
        connection.execute("INSERT INTO fills(value) VALUES ('source')")
        connection.commit()


def _write_sentinel_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE sentinel(value TEXT NOT NULL)")
        connection.execute("INSERT INTO sentinel(value) VALUES ('untouched')")
        connection.commit()


def _sqlite_spec() -> SnapshotSpec:
    return SnapshotSpec(
        logical_id="paper.db",
        owner="paper-execution",
        recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
        snapshot_kind=SnapshotKind.SQLITE,
        source="paper.sqlite3",
        restore_target="paper/state.sqlite3",
    )


def test_sqlite_snapshot_does_not_follow_predictable_temp_symlink(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    database = source / "paper.sqlite3"
    _write_source_database(database)

    outside = tmp_path / "outside.sqlite3"
    _write_sentinel_database(outside)
    payload = backup / "payload"
    payload.mkdir()
    legacy_temp = payload / ".paper.db.tmp"
    try:
        legacy_temp.symlink_to(outside)
    except OSError:
        pytest.skip("symlinks are unavailable")

    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=_sqlite_spec(),
    )

    verify_snapshot_file(backup, entry)
    assert legacy_temp.is_symlink()
    with sqlite3.connect(outside) as connection:
        assert connection.execute("SELECT value FROM sentinel").fetchall() == [("untouched",)]
    assert list(payload.glob(".paper.db.*.tmp")) == []


def test_invalid_sqlite_source_fails_safely_and_cleans_random_temp(tmp_path: Path) -> None:
    source, backup = _roots(tmp_path)
    (source / "paper.sqlite3").write_text("not a sqlite database\n", encoding="utf-8")

    with pytest.raises(RecoveryManifestError, match="SQLite snapshot failed safely"):
        snapshot_entry(
            source_root=source,
            backup_root=backup,
            spec=_sqlite_spec(),
        )

    payload = backup / "payload"
    assert not (payload / "paper.db").exists()
    assert list(payload.glob(".paper.db.*.tmp")) == []
