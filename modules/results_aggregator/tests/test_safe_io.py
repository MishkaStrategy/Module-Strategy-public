from __future__ import annotations

import os
from pathlib import Path

import pytest

import modular_results_registry.safe_io as safe_io
from modular_results_registry.errors import IntegrityError, ResourceLimitError, ValidationError
from modular_results_registry.safe_io import (
    atomic_write_bytes,
    atomic_write_text,
    ensure_safe_parent,
)


def test_atomic_write_bytes_and_text_are_durable(tmp_path: Path) -> None:
    binary = tmp_path / "nested" / "result.bin"
    atomic_write_bytes(binary, b"payload")
    assert binary.read_bytes() == b"payload"
    text = tmp_path / "nested" / "result.txt"
    atomic_write_text(text, "hello")
    assert text.read_text(encoding="utf-8") == "hello"


def test_atomic_write_rejects_existing_symlink_and_nonregular_targets(
    tmp_path: Path,
) -> None:
    real = tmp_path / "real.txt"
    real.write_text("real", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    with pytest.raises(IntegrityError):
        atomic_write_bytes(link, b"new")
    directory = tmp_path / "directory"
    directory.mkdir()
    with pytest.raises(IntegrityError):
        atomic_write_bytes(directory, b"new")


def test_atomic_write_rejects_unsafe_parent_components(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    symlink_parent = tmp_path / "link"
    symlink_parent.symlink_to(real, target_is_directory=True)
    with pytest.raises(IntegrityError):
        atomic_write_bytes(symlink_parent / "file", b"new")
    file_parent = tmp_path / "file-parent"
    file_parent.write_text("x", encoding="utf-8")
    with pytest.raises(IntegrityError):
        atomic_write_bytes(file_parent / "file", b"new")


def test_atomic_write_no_clobber_and_resource_limits(tmp_path: Path) -> None:
    path = tmp_path / "result"
    atomic_write_bytes(path, b"first")
    with pytest.raises(IntegrityError):
        atomic_write_bytes(path, b"second", replace_existing=False)
    with pytest.raises(ResourceLimitError):
        atomic_write_bytes(tmp_path / "large", b"abc", max_bytes=2)
    with pytest.raises(ValueError):
        atomic_write_bytes(tmp_path / "invalid", b"abc", max_bytes=0)
    with pytest.raises(TypeError):
        atomic_write_bytes(tmp_path / "invalid", "abc")  # type: ignore[arg-type]
    with pytest.raises(ValidationError):
        atomic_write_bytes(tmp_path / "invalid", b"abc", mode=0o1000)


def test_atomic_write_failure_cleans_temporary_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_link(*args: object, **kwargs: object) -> None:
        raise OSError("link failure")

    monkeypatch.setattr(safe_io.os, "link", fail_link)
    path = tmp_path / "result"
    with pytest.raises(IntegrityError, match="publication failed"):
        atomic_write_bytes(path, b"content")
    assert not path.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_fsync_parent_failure_rolls_back(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = safe_io.os.fsync
    calls = 0

    def fail_parent(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failure")
        original(descriptor)

    monkeypatch.setattr(safe_io.os, "fsync", fail_parent)
    path = tmp_path / "result"
    with pytest.raises(IntegrityError, match="publication failed"):
        atomic_write_bytes(path, b"content")
    assert not path.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_write_rollback_failure_is_reported(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fsync = safe_io.os.fsync
    original_unlink = safe_io.os.unlink
    calls = 0

    def fail_parent(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("directory fsync failure")
        original_fsync(descriptor)

    def fail_target_unlink(path: str, *args: object, **kwargs: object) -> None:
        if path == "result":
            raise OSError("rollback failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(safe_io.os, "fsync", fail_parent)
    monkeypatch.setattr(safe_io.os, "unlink", fail_target_unlink)
    with pytest.raises(IntegrityError, match="rollback was incomplete"):
        atomic_write_bytes(tmp_path / "result", b"content")


def test_atomic_write_failure_before_temp_name_is_assigned(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def fail_open(*args: object, **kwargs: object) -> int:
        raise OSError("cannot create temp")

    monkeypatch.setattr(safe_io.os, "open", fail_open)
    with pytest.raises(IntegrityError, match="cannot be opened"):
        atomic_write_bytes(tmp_path / "result", b"content")


def test_atomic_write_retries_temporary_name_collisions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = safe_io.os.open
    collisions = 0

    def collide_twice(path: str | bytes, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal collisions
        if isinstance(path, str) and path.startswith(".result.") and collisions < 2:
            collisions += 1
            raise FileExistsError("collision")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_io.os, "open", collide_twice)
    atomic_write_bytes(tmp_path / "result", b"content")
    assert collisions == 2
    assert (tmp_path / "result").read_bytes() == b"content"


def test_atomic_write_exhausted_temp_names_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = safe_io.os.open

    def always_collide(path: str | bytes, flags: int, *args: object, **kwargs: object) -> int:
        if isinstance(path, str) and path.startswith(".result."):
            raise FileExistsError("collision")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(safe_io.os, "open", always_collide)
    with pytest.raises(IntegrityError, match="cannot allocate"):
        atomic_write_bytes(tmp_path / "result", b"content")
    assert not (tmp_path / "result").exists()


def test_ensure_safe_parent_rejects_existing_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("x", encoding="utf-8")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(IntegrityError):
        ensure_safe_parent(link)


def test_atomic_write_rejects_target_created_during_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_lexists = safe_io.os.path.lexists
    calls = 0

    def race(path: str | os.PathLike[str]) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            target = Path(path)
            target.write_text("racer", encoding="utf-8")
            return False
        return original_lexists(path)

    monkeypatch.setattr(safe_io.os.path, "lexists", race)
    path = tmp_path / "result"
    with pytest.raises(IntegrityError, match="already exists"):
        atomic_write_bytes(path, b"content")
    assert path.read_text(encoding="utf-8") == "racer"


def test_atomic_write_existing_target_replace_fsync_failure_preserves_original(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "result"
    path.write_bytes(b"original")
    original_fsync = safe_io.os.fsync
    calls = 0

    def fail_parent(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        # Existing target: temp fsync, backup link fsync, promotion fsync.
        if calls == 3:
            raise OSError("promotion fsync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(safe_io.os, "fsync", fail_parent)
    with pytest.raises(IntegrityError, match="publication failed"):
        atomic_write_bytes(path, b"replacement")
    assert path.read_bytes() == b"original"
    assert not list(tmp_path.glob(".result.*.tmp"))
    assert not list(tmp_path.glob(".result.*.bak"))
