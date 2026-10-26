from __future__ import annotations

import hashlib
import os
from pathlib import Path

import pytest

from modular_results_registry.artifact_store import (
    FileLimits,
    ImmutableArtifactStore,
    verify_file,
)
from modular_results_registry.errors import IntegrityError, ResourceLimitError, ValidationError
from modular_results_registry.models import ArtifactRecord


def _record(data: bytes) -> ArtifactRecord:
    return ArtifactRecord(
        artifact_id="artifact-store-001",
        package_id="package-store-001",
        relative_path="data/file.bin",
        media_type="application/octet-stream",
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        producer_module_id="producer",
        producer_module_version="1",
        schema_id="binary",
        schema_version="1",
    )


def test_verify_file_and_content_addressed_import(tmp_path: Path) -> None:
    data = b"immutable artifact\n"
    source = tmp_path / "source.bin"
    source.write_bytes(data)
    record = _record(data)
    verified = verify_file(source, expected_sha256=record.sha256, expected_size=record.size_bytes)
    assert verified.sha256 == record.sha256
    store = ImmutableArtifactStore(tmp_path / "store")
    target, state = store.import_artifact(source, record)
    assert state == "IMPORTED"
    assert target.read_bytes() == data
    assert target.name == record.sha256
    assert target.stat().st_mode & 0o222 == 0
    target_again, state_again = store.import_artifact(source, record)
    assert (target_again, state_again) == (target, "DUPLICATE")
    inventory = store.verify_inventory()
    assert len(inventory) == 1 and inventory[0].sha256 == record.sha256


def test_verify_file_rejects_symlink_fifo_and_limits(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"abc")
    symlink = tmp_path / "link"
    symlink.symlink_to(source)
    with pytest.raises(IntegrityError):
        verify_file(symlink)
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(IntegrityError):
        verify_file(fifo)
    with pytest.raises(ResourceLimitError):
        verify_file(source, limits=FileLimits(max_file_bytes=2))


def test_import_detects_mismatch_and_store_corruption(tmp_path: Path) -> None:
    source = tmp_path / "source"
    source.write_bytes(b"abc")
    record = _record(b"different")
    store = ImmutableArtifactStore(tmp_path / "store")
    with pytest.raises(IntegrityError):
        store.import_artifact(source, record)

    good = _record(b"abc")
    target, _ = store.import_artifact(source, good)
    target.chmod(0o600)
    target.write_bytes(b"tampered")
    with pytest.raises(IntegrityError):
        store.import_artifact(source, good)


def test_store_rejects_symlink_root_and_bad_hash(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "store-link"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ValidationError):
        ImmutableArtifactStore(link)
    store = ImmutableArtifactStore(tmp_path / "store")
    with pytest.raises(ValidationError):
        store.target_for_hash("bad")


def test_inventory_rejects_invalid_layout_and_symlink(tmp_path: Path) -> None:
    store = ImmutableArtifactStore(tmp_path / "store")
    bad = store.root / "bad"
    bad.mkdir()
    (bad / "name").write_bytes(b"x")
    with pytest.raises(IntegrityError):
        store.verify_inventory()

    for path in sorted(store.root.rglob("*"), reverse=True):
        if path.is_file() or path.is_symlink():
            path.unlink()
        elif path.is_dir():
            path.rmdir()
    source = tmp_path / "source"
    source.write_bytes(b"abc")
    target, _ = store.import_artifact(source, _record(b"abc"))
    link = target.parent / ("f" * 64)
    link.symlink_to(target)
    with pytest.raises(IntegrityError):
        store.verify_inventory()
