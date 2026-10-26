from __future__ import annotations

import gzip
import hashlib
import importlib.util
import io
import sys
import tarfile
from pathlib import Path

import pytest

_TOOL_PATH = Path(__file__).parents[1] / "tools" / "normalize_sdist.py"
_SPEC = importlib.util.spec_from_file_location("normalize_sdist_tool", _TOOL_PATH)
if _SPEC is None or _SPEC.loader is None:
    raise RuntimeError("cannot load normalize_sdist tool")
_MODULE = importlib.util.module_from_spec(_SPEC)
sys.modules[_SPEC.name] = _MODULE
_SPEC.loader.exec_module(_MODULE)
SdistNormalizationError = _MODULE.SdistNormalizationError
normalize_sdist = _MODULE.normalize_sdist


def _archive(
    path: Path,
    *,
    order: tuple[str, ...],
    member_mtime: int,
    gzip_mtime: int,
    uid: int,
    uname: str,
) -> None:
    payloads = {
        "example-1.0": None,
        "example-1.0/PKG-INFO": b"Name: example\nVersion: 1.0\n",
        "example-1.0/src/example.py": b"VALUE = 1\n",
    }
    with path.open("wb") as raw_output:
        with gzip.GzipFile(
            filename="source-name.tar",
            mode="wb",
            fileobj=raw_output,
            mtime=gzip_mtime,
        ) as compressed_output:
            with tarfile.open(fileobj=compressed_output, mode="w") as archive:
                for name in order:
                    data = payloads[name]
                    member = tarfile.TarInfo(name)
                    member.mtime = member_mtime
                    member.uid = uid
                    member.gid = uid
                    member.uname = uname
                    member.gname = uname
                    member.mode = 0o755 if data is None else 0o644
                    member.type = tarfile.DIRTYPE if data is None else tarfile.REGTYPE
                    member.size = 0 if data is None else len(data)
                    archive.addfile(member, io.BytesIO(data) if data is not None else None)


def test_normalization_is_byte_reproducible_across_source_metadata(tmp_path: Path) -> None:
    first_source = tmp_path / "first.tar.gz"
    second_source = tmp_path / "second.tar.gz"
    names = (
        "example-1.0",
        "example-1.0/PKG-INFO",
        "example-1.0/src/example.py",
    )
    _archive(
        first_source,
        order=names,
        member_mtime=100,
        gzip_mtime=200,
        uid=501,
        uname="first-user",
    )
    _archive(
        second_source,
        order=tuple(reversed(names)),
        member_mtime=300,
        gzip_mtime=400,
        uid=1_000,
        uname="second-user",
    )

    first = normalize_sdist(first_source, tmp_path / "first-normalized.tar.gz", epoch=500)
    second = normalize_sdist(second_source, tmp_path / "second-normalized.tar.gz", epoch=500)

    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    with tarfile.open(first, mode="r:gz") as archive:
        members = archive.getmembers()
    assert [member.name for member in members] == sorted(names)
    assert all(member.mtime == 500 for member in members)
    assert all(member.uid == member.gid == 0 for member in members)
    assert all(member.uname == member.gname == "" for member in members)


def test_normalization_can_replace_source_atomically(tmp_path: Path) -> None:
    source = tmp_path / "source.tar.gz"
    names = ("example-1.0", "example-1.0/PKG-INFO", "example-1.0/src/example.py")
    _archive(source, order=names, member_mtime=10, gzip_mtime=20, uid=501, uname="user")

    result = normalize_sdist(source, epoch=30)

    assert result == source.resolve()
    with tarfile.open(source, mode="r:gz") as archive:
        assert [member.name for member in archive.getmembers()] == sorted(names)


@pytest.mark.parametrize("unsafe_name", ["../escape", "/absolute", "C:/drive", "bad\\path"])
def test_normalization_rejects_unsafe_member_paths(
    tmp_path: Path,
    unsafe_name: str,
) -> None:
    source = tmp_path / "unsafe.tar.gz"
    with tarfile.open(source, mode="w:gz") as archive:
        member = tarfile.TarInfo(unsafe_name)
        member.size = 1
        archive.addfile(member, io.BytesIO(b"x"))

    with pytest.raises(SdistNormalizationError, match="archive member path"):
        normalize_sdist(source)


def test_normalization_rejects_links_and_duplicate_names(tmp_path: Path) -> None:
    link_source = tmp_path / "link.tar.gz"
    with tarfile.open(link_source, mode="w:gz") as archive:
        member = tarfile.TarInfo("example/link")
        member.type = tarfile.SYMTYPE
        member.linkname = "target"
        archive.addfile(member)
    with pytest.raises(SdistNormalizationError, match="unsupported archive member type"):
        normalize_sdist(link_source)

    duplicate_source = tmp_path / "duplicate.tar.gz"
    with tarfile.open(duplicate_source, mode="w:gz") as archive:
        for value in (b"a", b"b"):
            member = tarfile.TarInfo("example/value.txt")
            member.size = len(value)
            archive.addfile(member, io.BytesIO(value))
    with pytest.raises(SdistNormalizationError, match="duplicate archive member path"):
        normalize_sdist(duplicate_source)
