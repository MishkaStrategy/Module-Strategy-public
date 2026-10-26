from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import modular_results_registry.recovery as recovery
from modular_results_registry.canonical import canonical_json_bytes, canonical_sha256
from modular_results_registry.errors import IntegrityError, RecoveryError


def _manifest() -> dict[str, object]:
    value: dict[str, object] = {
        "schema_id": "modular-results-registry.backup",
        "schema_version": "1",
        "database_filename": "backup.sqlite",
        "database_sha256": "a" * 64,
        "database_size": 10,
        "registry_revision": 1,
        "entity_state_hash": "b" * 64,
        "verified_state_hash": "c" * 64,
    }
    value["manifest_hash"] = canonical_sha256(value)
    return value


def _write_manifest(path: Path, value: dict[str, object] | None = None) -> None:
    document = _manifest() if value is None else value
    path.write_bytes(canonical_json_bytes(document))


def test_safe_parent_and_directory_wrappers_translate_integrity_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail(*args, **kwargs):
        raise IntegrityError("unsafe")

    monkeypatch.setattr(recovery, "_open_directory_chain_nofollow", fail)
    with pytest.raises(RecoveryError, match="path parent"):
        recovery._ensure_safe_parent(tmp_path / "a" / "file")
    with pytest.raises(RecoveryError, match="destination parent"):
        recovery._open_directory_nofollow(tmp_path)


def test_open_regular_nofollow_rejects_open_error_nonregular_and_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(RecoveryError, match="cannot be opened safely"):
        recovery._open_regular_nofollow(missing)

    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(RecoveryError, match="must be a regular file"):
        recovery._open_regular_nofollow(directory)

    source = tmp_path / "source"
    source.write_bytes(b"abc")
    original_fstat = recovery.os.fstat

    def interrupt(fd: int):
        value = original_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            raise KeyboardInterrupt("stop")
        return value

    monkeypatch.setattr(recovery.os, "fstat", interrupt)
    with pytest.raises(KeyboardInterrupt):
        recovery._open_regular_nofollow(source)


def test_publish_temp_requires_same_parent_regular_temp_and_absent_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    temp = first / "temp"
    temp.write_bytes(b"data")
    with pytest.raises(RecoveryError, match="share the destination parent"):
        recovery._publish_temp_noreplace(temp, second / "target")

    missing = first / "missing"
    with pytest.raises(RecoveryError, match="temporary file cannot be inspected"):
        recovery._publish_temp_noreplace(missing, first / "target")

    directory = first / "temp-directory"
    directory.mkdir()
    with pytest.raises(RecoveryError, match="temporary file must be regular"):
        recovery._publish_temp_noreplace(directory, first / "target")

    target = first / "target"
    target.write_bytes(b"winner")
    with pytest.raises(RecoveryError, match="destination already exists"):
        recovery._publish_temp_noreplace(temp, target)
    target.unlink()

    original_stat = recovery.os.stat

    def fail_target(path, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            raise PermissionError("inspect denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(recovery.os, "stat", fail_target)
    with pytest.raises(RecoveryError, match="destination cannot be inspected"):
        recovery._publish_temp_noreplace(temp, target)


def test_publish_temp_detects_race_and_rolls_back_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    temp = tmp_path / "temp"
    target = tmp_path / "target"
    temp.write_bytes(b"data")
    original_link = recovery.os.link

    def race(*args, **kwargs):
        raise FileExistsError("race")

    monkeypatch.setattr(recovery.os, "link", race)
    with pytest.raises(RecoveryError, match="destination already exists"):
        recovery._publish_temp_noreplace(temp, target)
    assert temp.exists() and not target.exists()

    monkeypatch.setattr(recovery.os, "link", original_link)
    original_fsync = recovery.os.fsync
    failed = False

    def fail_directory_once(fd: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("publish fsync")
        original_fsync(fd)

    monkeypatch.setattr(recovery.os, "fsync", fail_directory_once)
    with pytest.raises(RecoveryError, match="publication failed"):
        recovery._publish_temp_noreplace(temp, target)
    assert temp.exists() and not target.exists()

    monkeypatch.setattr(recovery.os, "fsync", original_fsync)
    original_unlink = recovery.os.unlink
    failed = False

    def fail_directory_again(fd: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("publish fsync")
        original_fsync(fd)

    def fail_target_unlink(path, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            raise OSError("rollback denied")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(recovery.os, "fsync", fail_directory_again)
    monkeypatch.setattr(recovery.os, "unlink", fail_target_unlink)
    with pytest.raises(RecoveryError, match="rollback was incomplete"):
        recovery._publish_temp_noreplace(temp, target)
    assert target.exists()


def test_remove_published_file_handles_missing_nonregular_and_unlink_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "missing"
    recovery._remove_published_file(target)

    target.mkdir()
    with pytest.raises(RecoveryError, match="no longer a regular file"):
        recovery._remove_published_file(target)
    target.rmdir()

    target.write_bytes(b"data")
    original_unlink = recovery.os.unlink

    def fail(*args, **kwargs):
        raise PermissionError("denied")

    monkeypatch.setattr(recovery.os, "unlink", fail)
    with pytest.raises(RecoveryError, match="rollback failed"):
        recovery._remove_published_file(target)
    monkeypatch.setattr(recovery.os, "unlink", original_unlink)
    recovery._remove_published_file(target)
    assert not target.exists()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda v: v.update(extra=True), "fields are invalid"),
        (lambda v: v.update(schema_id="wrong"), "unsupported backup manifest schema"),
        (lambda v: v.update(schema_version="2"), "unsupported backup manifest schema"),
        (lambda v: v.update(database_filename=""), "database_filename is invalid"),
        (lambda v: v.update(database_filename="../backup.sqlite"), "database_filename is invalid"),
        (lambda v: v.update(database_filename="."), "database_filename is invalid"),
        (lambda v: v.update(database_sha256="bad"), "database_sha256 is invalid"),
        (lambda v: v.update(entity_state_hash="A" * 64), "entity_state_hash is invalid"),
        (lambda v: v.update(verified_state_hash=1), "verified_state_hash is invalid"),
        (lambda v: v.update(manifest_hash="g" * 64), "manifest_hash is invalid"),
        (lambda v: v.update(database_size=True), "database_size is invalid"),
        (lambda v: v.update(database_size=-1), "database_size is invalid"),
        (lambda v: v.update(registry_revision=1.5), "registry_revision is invalid"),
    ],
)
def test_validate_backup_manifest_is_fail_closed(mutate, message: str) -> None:
    value = _manifest()
    mutate(value)
    with pytest.raises(RecoveryError, match=message):
        recovery._validate_backup_manifest(value)


def test_sha256_file_detects_source_change(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"abc")
    original_fstat = recovery.os.fstat
    regular_calls = 0

    def changed(fd: int):
        nonlocal regular_calls
        value = original_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            regular_calls += 1
            if regular_calls >= 2:
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns + 1,
                )
        return value

    monkeypatch.setattr(recovery.os, "fstat", changed)
    with pytest.raises(RecoveryError, match="changed during hashing"):
        recovery._sha256_file(source)


def test_load_manifest_rejects_size_invalid_json_nonobject_and_hash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    _write_manifest(path)
    monkeypatch.setattr(recovery, "_MAX_MANIFEST_BYTES", 1)
    with pytest.raises(RecoveryError, match="exceeds size limit"):
        recovery._load_manifest(path)

    monkeypatch.setattr(recovery, "_MAX_MANIFEST_BYTES", 4 * 1024 * 1024)
    for payload, message in (
        (b"\xff", "unreadable"),
        (b'{"value":NaN}', "unreadable"),
        (b'{"broken":', "unreadable"),
        (b"[]", "must be an object"),
    ):
        path.write_bytes(payload)
        with pytest.raises(RecoveryError, match=message):
            recovery._load_manifest(path)

    value = _manifest()
    value["database_size"] = 11
    path.write_bytes(canonical_json_bytes(value))
    with pytest.raises(RecoveryError, match="hash mismatch"):
        recovery._load_manifest(path)


def test_load_manifest_detects_growth_while_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "manifest.json"
    path.write_bytes(b"{}")
    monkeypatch.setattr(recovery, "_MAX_MANIFEST_BYTES", 2)
    original_read = recovery.os.read
    original_fstat = recovery.os.fstat
    injected = False

    def overread(fd: int, amount: int) -> bytes:
        nonlocal injected
        chunk = original_read(fd, amount)
        if stat.S_ISREG(original_fstat(fd).st_mode) and not chunk and not injected:
            injected = True
            return b"x"
        return chunk

    monkeypatch.setattr(recovery.os, "read", overread)
    with pytest.raises(RecoveryError, match="exceeds size limit"):
        recovery._load_manifest(path)
