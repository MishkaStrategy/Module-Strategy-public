from __future__ import annotations

import gzip
import hashlib
import io
import tarfile
from pathlib import Path

import pytest

from scripts.normalize_sdist import normalize_sdist

EPOCH = 946_684_800


def _write_archive(
    path: Path,
    *,
    gzip_mtime: int,
    member_mtime: int,
    member_name: str = "example-1.0/module.py",
) -> None:
    with path.open("wb") as raw:
        with gzip.GzipFile(
            filename="",
            mode="wb",
            fileobj=raw,
            mtime=gzip_mtime,
        ) as compressed:
            with tarfile.open(
                fileobj=compressed,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as archive:
                directory = tarfile.TarInfo("example-1.0")
                directory.type = tarfile.DIRTYPE
                directory.mode = 0o755
                directory.uid = 501
                directory.gid = 20
                directory.uname = "builder"
                directory.gname = "staff"
                directory.mtime = member_mtime
                archive.addfile(directory)

                payload = b"VALUE = 1\n"
                source = tarfile.TarInfo(member_name)
                source.mode = 0o644
                source.uid = 501
                source.gid = 20
                source.uname = "builder"
                source.gname = "staff"
                source.mtime = member_mtime
                source.size = len(payload)
                archive.addfile(source, io.BytesIO(payload))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_normalization_makes_equivalent_sdists_byte_identical(
    tmp_path: Path,
) -> None:
    first = tmp_path / "first.tar.gz"
    second = tmp_path / "second.tar.gz"
    _write_archive(first, gzip_mtime=100, member_mtime=200)
    _write_archive(second, gzip_mtime=300, member_mtime=400)

    normalize_sdist(first, epoch=EPOCH)
    normalize_sdist(second, epoch=EPOCH)

    assert _sha256(first) == _sha256(second)
    with tarfile.open(first, mode="r:gz") as archive:
        members = archive.getmembers()
        assert [member.name for member in members] == [
            "example-1.0",
            "example-1.0/module.py",
        ]
        assert all(member.mtime == EPOCH for member in members)
        assert all(member.uid == 0 and member.gid == 0 for member in members)
        assert all(member.uname == "" and member.gname == "" for member in members)
        source = archive.extractfile("example-1.0/module.py")
        assert source is not None
        assert source.read() == b"VALUE = 1\n"


def test_normalization_rejects_unsafe_member_path(tmp_path: Path) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    _write_archive(
        archive,
        gzip_mtime=100,
        member_mtime=200,
        member_name="../escape.py",
    )

    with pytest.raises(ValueError, match="unsafe sdist member path"):
        normalize_sdist(archive, epoch=EPOCH)


def test_normalization_rejects_symlink_input(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    link = tmp_path / "link.tar.gz"
    _write_archive(archive, gzip_mtime=100, member_mtime=200)
    link.symlink_to(archive)

    with pytest.raises(ValueError, match="regular file"):
        normalize_sdist(link, epoch=EPOCH)


def test_normalization_rejects_negative_epoch(tmp_path: Path) -> None:
    archive = tmp_path / "source.tar.gz"
    _write_archive(archive, gzip_mtime=100, member_mtime=200)

    with pytest.raises(ValueError, match="cannot be negative"):
        normalize_sdist(archive, epoch=-1)
