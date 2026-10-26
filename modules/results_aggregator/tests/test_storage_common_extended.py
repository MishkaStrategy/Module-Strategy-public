from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import modular_results_registry.storage_common as common
from modular_results_registry.canonical import canonical_json_bytes
from modular_results_registry.errors import IntegrityError


def test_decode_canonical_json_object_rejects_invalid_nonobject_and_noncanonical() -> None:
    value, raw = common._decode_canonical_json_object(
        canonical_json_bytes({"a": 1}), context="payload"
    )
    assert value == {"a": 1}
    assert raw == b'{"a":1}'
    for payload in (object(), b"\xff", b'{"a":NaN}', b'{"a":'):
        with pytest.raises(IntegrityError, match="not valid JSON"):
            common._decode_canonical_json_object(payload, context="payload")
    with pytest.raises(IntegrityError, match="not a JSON object"):
        common._decode_canonical_json_object(b"[]", context="payload")
    with pytest.raises(IntegrityError, match="not canonically encoded"):
        common._decode_canonical_json_object(b'{"a": 1}', context="payload")


def test_sql_identifier_allowlist_is_fail_closed() -> None:
    assert common._sql_identifier("artifacts") == "artifacts"
    assert common._sql_identifier_list(("artifact_id", "sha256")) == "artifact_id,sha256"
    with pytest.raises(IntegrityError, match="unsafe SQL identifier"):
        common._sql_identifier("artifacts; DROP TABLE artifacts")


def test_open_registry_guard_wraps_missing_parent_and_parent_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(IntegrityError, match="parent cannot be opened safely"):
        common._open_registry_guard(tmp_path / "missing" / "registry.sqlite")

    target = tmp_path / "registry.sqlite"
    original_lstat = common.os.lstat

    def changed_parent(path):
        value = original_lstat(path)
        if Path(path) == target.parent:
            return SimpleNamespace(
                st_mode=value.st_mode,
                st_dev=value.st_dev,
                st_ino=value.st_ino + 1,
            )
        return value

    monkeypatch.setattr(common.os, "lstat", changed_parent)
    with pytest.raises(IntegrityError, match="parent changed"):
        common._open_registry_guard(target)


def test_open_registry_guard_handles_create_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "registry.sqlite"
    original_open = common.os.open
    injected = False

    def race_create(path, flags, *args, **kwargs):
        nonlocal injected
        if (
            path == target.name
            and flags & os.O_CREAT
            and kwargs.get("dir_fd") is not None
            and not injected
        ):
            injected = True
            descriptor = original_open(path, flags, *args, **kwargs)
            os.close(descriptor)
            raise FileExistsError("concurrent creator")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(common.os, "open", race_create)
    parent_fd, descriptor, info, created = common._open_registry_guard(target)
    try:
        assert not created
        assert stat.S_ISREG(info.st_mode)
    finally:
        os.close(descriptor)
        os.close(parent_fd)


def test_open_registry_guard_rejects_descriptor_type_and_closes_on_errors(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "registry.sqlite"
    target.write_bytes(b"")
    original_fstat = common.os.fstat
    regular_calls = 0

    def nonregular(fd: int):
        nonlocal regular_calls
        value = original_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            regular_calls += 1
            return SimpleNamespace(
                st_mode=stat.S_IFDIR | 0o700,
                st_dev=value.st_dev,
                st_ino=value.st_ino,
            )
        return value

    monkeypatch.setattr(common.os, "fstat", nonregular)
    with pytest.raises(IntegrityError, match="regular file"):
        common._open_registry_guard(target)

    monkeypatch.setattr(common.os, "fstat", original_fstat)
    original_open = common.os.open

    def fail_target(path, flags, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            raise PermissionError("denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(common.os, "open", fail_target)
    with pytest.raises(IntegrityError, match="cannot be opened safely"):
        common._open_registry_guard(target)


def test_open_registry_guard_closes_on_base_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "registry.sqlite"
    target.write_bytes(b"")
    original_fstat = common.os.fstat

    def interrupt_regular(fd: int):
        value = original_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            raise KeyboardInterrupt("stop")
        return value

    monkeypatch.setattr(common.os, "fstat", interrupt_regular)
    with pytest.raises(KeyboardInterrupt):
        common._open_registry_guard(target)


def test_same_registry_entry_and_sidecars_are_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "registry.sqlite"
    target.write_bytes(b"")
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        expected = target.stat()
        assert common._same_registry_entry(parent_fd, target, expected)
        target.unlink()
        assert not common._same_registry_entry(parent_fd, target, expected)
    finally:
        os.close(parent_fd)

    target.write_bytes(b"")
    sidecar = Path(str(target) + "-wal")
    real = tmp_path / "real-wal"
    real.write_bytes(b"")
    sidecar.symlink_to(real)
    with pytest.raises(IntegrityError, match="sidecar must be a regular file"):
        common._validate_sqlite_sidecars(target)
    sidecar.unlink()

    sidecar.write_bytes(b"")
    original_lstat = common.os.lstat

    def fail_sidecar(path):
        if Path(path) == sidecar:
            raise PermissionError("denied")
        return original_lstat(path)

    monkeypatch.setattr(common.os.path, "lexists", lambda path: Path(path) == sidecar)
    monkeypatch.setattr(common.os, "lstat", fail_sidecar)
    with pytest.raises(IntegrityError, match="cannot be inspected safely"):
        common._validate_sqlite_sidecars(target)


def test_cleanup_created_registry_only_removes_same_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "registry.sqlite"
    target.write_bytes(b"")
    parent_fd = os.open(tmp_path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        info = target.stat()
        common._cleanup_created_registry(parent_fd, target, info, False)
        assert target.exists()
        different = SimpleNamespace(st_dev=info.st_dev, st_ino=info.st_ino + 1)
        common._cleanup_created_registry(parent_fd, target, different, True)
        assert target.exists()

        original_unlink = common.os.unlink

        def fail_unlink(*args, **kwargs):
            raise OSError("cleanup failure")

        monkeypatch.setattr(common.os, "unlink", fail_unlink)
        common._cleanup_created_registry(parent_fd, target, info, True)
        assert target.exists()
        monkeypatch.setattr(common.os, "unlink", original_unlink)

        common._cleanup_created_registry(parent_fd, target, info, True)
        assert not target.exists()
    finally:
        os.close(parent_fd)
