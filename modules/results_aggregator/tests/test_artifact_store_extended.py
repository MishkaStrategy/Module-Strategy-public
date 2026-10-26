from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import modular_results_registry.artifact_store as artifact_store
from modular_results_registry.artifact_store import (
    FileLimits,
    ImmutableArtifactStore,
    VerifiedFile,
    read_verified_bytes,
    verify_file,
)
from modular_results_registry.errors import IntegrityError, ResourceLimitError, ValidationError
from modular_results_registry.models import ArtifactRecord


def _record(data: bytes, *, artifact_id: str = "artifact-extended") -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id=artifact_id,
        package_id="package-extended",
        relative_path="evidence/payload.bin",
        media_type="application/octet-stream",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        producer_module_id="producer",
        producer_module_version="1",
        schema_id="binary",
        schema_version="1",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_file_bytes": True},
        {"max_file_bytes": 0},
        {"max_file_bytes": 1.5},
        {"chunk_bytes": True},
        {"chunk_bytes": 4095},
        {"chunk_bytes": 16 * 1024 * 1024 + 1},
        {"chunk_bytes": "4096"},
    ],
)
def test_file_limits_reject_invalid_values(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FileLimits(**kwargs)  # type: ignore[arg-type]


def test_verify_and_read_validate_arguments(tmp_path: Path) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    bad_hashes: tuple[object, ...] = (123, "a" * 63, "A" * 64, "g" * 64)
    for digest in bad_hashes:
        with pytest.raises(ValidationError, match="expected_sha256"):
            verify_file(source, expected_sha256=digest)  # type: ignore[arg-type]
    for size in (True, -1, 1.5, "3"):
        with pytest.raises(ValidationError, match="expected_size"):
            verify_file(source, expected_size=size)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="limits"):
        verify_file(source, limits=object())  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="collect_bytes"):
        artifact_store._verify_and_read(
            source,
            expected_sha256=None,
            expected_size=None,
            limits=FileLimits(),
            collect_bytes=1,  # type: ignore[arg-type]
        )
    with pytest.raises(IntegrityError, match="size mismatch"):
        verify_file(source, expected_size=4)
    with pytest.raises(IntegrityError, match="SHA-256 mismatch"):
        verify_file(source, expected_sha256="0" * 64)
    verified, data = read_verified_bytes(source, expected_size=3)
    assert verified.size_bytes == 3
    assert data == b"abc"


def test_directory_chain_rejects_missing_file_component_and_symlink(tmp_path: Path) -> None:
    missing = tmp_path / "missing" / "child"
    with pytest.raises(IntegrityError, match="does not exist"):
        artifact_store._open_directory_chain_nofollow(missing)

    component = tmp_path / "component"
    component.write_text("not a directory", encoding="utf-8")
    with pytest.raises(IntegrityError, match="unsafe directory component|not a directory"):
        artifact_store._open_directory_chain_nofollow(component / "child")

    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(IntegrityError, match="unsafe directory component"):
        artifact_store._open_directory_chain_nofollow(link / "child", create=True)


def test_directory_chain_wraps_root_and_creation_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_open = artifact_store.os.open

    def fail_root(path, flags, *args, **kwargs):
        if path == os.sep and kwargs.get("dir_fd") is None:
            raise PermissionError("root denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "open", fail_root)
    with pytest.raises(IntegrityError, match="root cannot be opened"):
        artifact_store._open_directory_chain_nofollow(tmp_path)

    monkeypatch.setattr(artifact_store.os, "open", original_open)
    original_mkdir = artifact_store.os.mkdir

    def fail_child(path, *args, **kwargs):
        if path == "new-child":
            raise PermissionError("mkdir denied")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "mkdir", fail_child)
    with pytest.raises(IntegrityError, match="cannot be created safely"):
        artifact_store._open_directory_chain_nofollow(tmp_path / "new-child", create=True)


def test_open_regular_detects_open_race_and_descriptor_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    original_open = artifact_store.os.open

    def fail_regular(path, flags, *args, **kwargs):
        if path == source.name and kwargs.get("dir_fd") is not None:
            raise PermissionError("open denied")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "open", fail_regular)
    with pytest.raises(IntegrityError, match="cannot open artifact safely"):
        verify_file(source)

    monkeypatch.setattr(artifact_store.os, "open", original_open)
    original_fstat = artifact_store.os.fstat
    regular_calls = 0

    def mismatch_inode(fd: int):
        nonlocal regular_calls
        value = original_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            regular_calls += 1
            if regular_calls == 1:
                return SimpleNamespace(
                    st_mode=value.st_mode,
                    st_dev=value.st_dev,
                    st_ino=value.st_ino + 1,
                    st_size=value.st_size,
                    st_mtime_ns=value.st_mtime_ns,
                )
        return value

    monkeypatch.setattr(artifact_store.os, "fstat", mismatch_inode)
    with pytest.raises(IntegrityError, match="changed while being opened"):
        verify_file(source)


def test_verify_detects_descriptor_mutation_and_stream_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.bin"
    source.write_bytes(b"abc")
    original_fstat = artifact_store.os.fstat
    regular_calls = 0

    def changed_after_read(fd: int):
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

    monkeypatch.setattr(artifact_store.os, "fstat", changed_after_read)
    with pytest.raises(IntegrityError, match="changed during verification"):
        verify_file(source)

    monkeypatch.setattr(artifact_store.os, "fstat", original_fstat)
    original_read = artifact_store.os.read
    regular_fd: int | None = None
    injected = False

    def overread(fd: int, amount: int) -> bytes:
        nonlocal regular_fd, injected
        value = original_fstat(fd)
        if stat.S_ISREG(value.st_mode):
            regular_fd = fd
        chunk = original_read(fd, amount)
        if fd == regular_fd and not chunk and not injected:
            injected = True
            return b"x"
        return chunk

    monkeypatch.setattr(artifact_store.os, "read", overread)
    with pytest.raises(ResourceLimitError, match="while reading"):
        verify_file(source, limits=FileLimits(max_file_bytes=3, chunk_bytes=4096))


def test_store_constructor_and_hash_validation(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="limits"):
        ImmutableArtifactStore(tmp_path / "store", limits=object())  # type: ignore[arg-type]
    occupied = tmp_path / "occupied"
    occupied.write_text("file", encoding="utf-8")
    with pytest.raises(ValidationError, match="safe real directory"):
        ImmutableArtifactStore(occupied)
    store = ImmutableArtifactStore(tmp_path / "store")
    for value in (None, 1, "A" * 64, "f" * 63, "g" * 64):
        with pytest.raises(ValidationError):
            store.target_for_hash(value)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="ArtifactRecord"):
        store.import_artifact(tmp_path / "missing", object())  # type: ignore[arg-type]


def test_import_rejects_target_symlink_and_inspection_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"payload"
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    record = _record(data)
    store = ImmutableArtifactStore(tmp_path / "store")
    target = store.target_for_hash(record.sha256)
    target.parent.mkdir(parents=True)
    victim = tmp_path / "victim"
    victim.write_bytes(data)
    target.symlink_to(victim)
    with pytest.raises(IntegrityError, match="target cannot be a symlink"):
        store.import_artifact(source, record)
    target.unlink()

    original_stat = artifact_store.os.stat

    def fail_target(path, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            raise PermissionError("stat denied")
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "stat", fail_target)
    with pytest.raises(IntegrityError, match="cannot be inspected"):
        store.import_artifact(source, record)


def test_import_temporary_name_exhaustion_cleans_store(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"payload"
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    record = _record(data)
    store = ImmutableArtifactStore(tmp_path / "store")
    target = store.target_for_hash(record.sha256)
    target.parent.mkdir(parents=True)
    original_open = artifact_store.os.open

    def collide(path, flags, *args, **kwargs):
        if isinstance(path, str) and path.startswith(f".{target.name}.") and path.endswith(".tmp"):
            raise FileExistsError("collision")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "open", collide)
    with pytest.raises(IntegrityError, match="cannot allocate"):
        store.import_artifact(source, record)
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))


def test_import_detects_copy_limit_mismatch_and_source_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"abc"
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    record = _record(data)
    store = ImmutableArtifactStore(
        tmp_path / "store", limits=FileLimits(max_file_bytes=3, chunk_bytes=4096)
    )

    monkeypatch.setattr(
        artifact_store,
        "verify_file",
        lambda *args, **kwargs: VerifiedFile(source, len(data), record.sha256),
    )
    original_read = artifact_store.os.read
    original_fstat = artifact_store.os.fstat
    read_state: dict[int, int] = {}

    def extra_copy_byte(fd: int, amount: int) -> bytes:
        info = original_fstat(fd)
        chunk = original_read(fd, amount)
        if stat.S_ISREG(info.st_mode) and not chunk and read_state.get(fd, 0) == 0:
            read_state[fd] = 1
            return b"x"
        return chunk

    monkeypatch.setattr(artifact_store.os, "read", extra_copy_byte)
    with pytest.raises(ResourceLimitError, match="while copying"):
        store.import_artifact(source, record)

    monkeypatch.setattr(artifact_store.os, "read", original_read)
    source.write_bytes(b"xyz")
    with pytest.raises(IntegrityError, match="does not match manifest"):
        store.import_artifact(source, record)

    source.write_bytes(data)
    regular_calls = 0

    def changed_source(fd: int):
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

    monkeypatch.setattr(artifact_store.os, "fstat", changed_source)
    with pytest.raises(IntegrityError, match="changed during copy"):
        store.import_artifact(source, record)


def test_import_race_returns_duplicate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data = b"payload"
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    record = _record(data)
    store = ImmutableArtifactStore(tmp_path / "store")
    target = store.target_for_hash(record.sha256)
    original_link = artifact_store.os.link
    raced = False

    def race_link(src, dst, *args, **kwargs):
        nonlocal raced
        if dst == target.name and not raced:
            raced = True
            original_link(src, dst, *args, **kwargs)
            raise FileExistsError("race winner")
        return original_link(src, dst, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "link", race_link)
    result, state = store.import_artifact(source, record)
    assert (result, state) == (target, "DUPLICATE")
    assert target.read_bytes() == data
    assert not list(target.parent.glob("*.tmp"))


def test_publication_failure_rolls_back_and_reports_incomplete_rollback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = b"payload"
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    record = _record(data)
    store = ImmutableArtifactStore(tmp_path / "store")
    target = store.target_for_hash(record.sha256)
    target.parent.mkdir(parents=True)
    original_fsync = artifact_store.os.fsync
    failed = False

    def fail_first_directory_fsync(fd: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("directory fsync failed")
        original_fsync(fd)

    monkeypatch.setattr(artifact_store.os, "fsync", fail_first_directory_fsync)
    with pytest.raises(IntegrityError, match="publication failed"):
        store.import_artifact(source, record)
    assert not target.exists()
    assert not list(target.parent.glob("*.tmp"))

    monkeypatch.setattr(artifact_store.os, "fsync", original_fsync)
    original_unlink = artifact_store.os.unlink
    failed = False

    def fail_again(fd: int) -> None:
        nonlocal failed
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not failed:
            failed = True
            raise OSError("directory fsync failed")
        original_fsync(fd)

    def fail_target_unlink(path, *args, **kwargs):
        if path == target.name and kwargs.get("dir_fd") is not None:
            raise OSError("rollback unlink failed")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(artifact_store.os, "fsync", fail_again)
    monkeypatch.setattr(artifact_store.os, "unlink", fail_target_unlink)
    with pytest.raises(IntegrityError, match="rollback was incomplete"):
        store.import_artifact(source, record)
    assert target.exists()


def test_inventory_rejects_symlink_directory_fifo_wrong_path_and_corruption(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "store")
    real = tmp_path / "real-dir"
    real.mkdir()
    (store.root / "link-dir").symlink_to(real, target_is_directory=True)
    with pytest.raises(IntegrityError, match="symlink directory"):
        store.verify_inventory()
    (store.root / "link-dir").unlink()

    fifo = store.root / ("a" * 64)
    os.mkfifo(fifo)
    with pytest.raises(IntegrityError, match="non-regular"):
        store.verify_inventory()
    fifo.unlink()

    data = b"payload"
    digest = hashlib.sha256(data).hexdigest()
    wrong = store.root / digest[:2] / "zz" / digest
    wrong.parent.mkdir(parents=True)
    wrong.write_bytes(data)
    with pytest.raises(IntegrityError, match="outside its content-addressed path"):
        store.verify_inventory()
    wrong.unlink()
    wrong.parent.rmdir()

    source = tmp_path / "source.bin"
    source.write_bytes(data)
    target, _ = store.import_artifact(source, _record(data))
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    with pytest.raises(IntegrityError, match="SHA-256 mismatch|size mismatch"):
        store.verify_inventory()
