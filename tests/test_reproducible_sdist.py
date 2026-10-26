from __future__ import annotations

import gzip
import importlib.util
import io
import tarfile
from pathlib import Path
from types import ModuleType
from typing import cast

import pytest


def _load_normalizer() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts" / "normalize_sdist.py"
    spec = importlib.util.spec_from_file_location("normalize_sdist", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load normalizer from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_NORMALIZER = _load_normalizer()
SdistNormalizationError = cast(type[ValueError], _NORMALIZER.SdistNormalizationError)
normalize_sdist = _NORMALIZER.normalize_sdist
normalized_sdist_bytes = _NORMALIZER.normalized_sdist_bytes


def _write_archive(
    path: Path,
    *,
    timestamp: int,
    reverse_order: bool,
    owner: tuple[int, int, str, str],
    names: tuple[str, ...] = (
        "package-1.0",
        "package-1.0/PKG-INFO",
        "package-1.0/src/module.py",
    ),
) -> None:
    payloads = {
        names[0]: None,
        names[1]: b"Name: package\nVersion: 1.0\n",
        names[2]: b"VALUE = 1\n",
    }
    members = list(names)
    if reverse_order:
        members.reverse()

    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for name in members:
            payload = payloads[name]
            member = tarfile.TarInfo(name)
            member.mtime = timestamp
            member.uid, member.gid, member.uname, member.gname = owner
            member.mode = 0o755 if payload is None else 0o644
            if payload is None:
                member.type = tarfile.DIRTYPE
                archive.addfile(member)
            else:
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))

    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename=path.name,
            mode="wb",
            fileobj=raw,
            mtime=timestamp,
        ) as compressed:
            compressed.write(tar_buffer.getvalue())


def _write_duplicate_archive(path: Path) -> None:
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for payload in (b"first", b"second"):
            member = tarfile.TarInfo("package-1.0/PKG-INFO")
            member.size = len(payload)
            archive.addfile(member, io.BytesIO(payload))
    with path.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=1) as compressed:
            compressed.write(tar_buffer.getvalue())


def test_normalization_removes_build_time_and_owner_variability(tmp_path: Path) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _write_archive(
        first,
        timestamp=100,
        reverse_order=False,
        owner=(501, 20, "builder-a", "staff"),
    )
    _write_archive(
        second,
        timestamp=200,
        reverse_order=True,
        owner=(1000, 1000, "builder-b", "users"),
    )

    first_bytes = normalized_sdist_bytes(first, epoch=42)
    second_bytes = normalized_sdist_bytes(second, epoch=42)

    assert first_bytes == second_bytes
    assert int.from_bytes(first_bytes[4:8], "little") == 42
    with tarfile.open(fileobj=io.BytesIO(first_bytes), mode="r:gz") as archive:
        names = [member.name for member in archive.getmembers()]
        assert names == sorted(names)
        for member in archive.getmembers():
            assert member.mtime == 42
            assert member.uid == 0
            assert member.gid == 0
            assert member.uname == ""
            assert member.gname == ""
            assert "mtime" not in member.pax_headers


def test_in_place_normalization_is_atomic_and_idempotent(tmp_path: Path) -> None:
    archive = tmp_path / "package.tar.gz"
    _write_archive(
        archive,
        timestamp=100,
        reverse_order=False,
        owner=(501, 20, "builder", "staff"),
    )

    normalize_sdist(archive, archive, epoch=42)
    once = archive.read_bytes()
    normalize_sdist(archive, archive, epoch=42)

    assert archive.read_bytes() == once
    assert archive.stat().st_mode & 0o777 == 0o644
    assert not list(tmp_path.glob(".*.tmp"))


def test_invalid_archives_epochs_and_paths_are_rejected(tmp_path: Path) -> None:
    invalid = tmp_path / "invalid.tar.gz"
    invalid.write_bytes(b"not-an-archive")

    with pytest.raises(SdistNormalizationError, match="invalid gzip-compressed sdist"):
        normalized_sdist_bytes(invalid, epoch=42)
    with pytest.raises(SdistNormalizationError, match="non-negative"):
        normalized_sdist_bytes(invalid, epoch=-1)
    with pytest.raises(TypeError, match="integer"):
        normalized_sdist_bytes(invalid, epoch=True)

    unsafe = tmp_path / "unsafe.tar.gz"
    _write_archive(
        unsafe,
        timestamp=100,
        reverse_order=False,
        owner=(501, 20, "builder", "staff"),
        names=("package-1.0", "../PKG-INFO", "package-1.0/src/module.py"),
    )
    with pytest.raises(SdistNormalizationError, match="unsafe sdist member path"):
        normalized_sdist_bytes(unsafe, epoch=42)

    duplicate = tmp_path / "duplicate.tar.gz"
    _write_duplicate_archive(duplicate)
    with pytest.raises(SdistNormalizationError, match="duplicate sdist member path"):
        normalized_sdist_bytes(duplicate, epoch=42)
