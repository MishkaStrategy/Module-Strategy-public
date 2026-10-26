from __future__ import annotations

import argparse
import gzip
import os
import posixpath
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_MAX_MEMBERS = 10_000
_MAX_TOTAL_FILE_BYTES = 1 << 30


class SdistNormalizationError(ValueError):
    """Raised when an input archive is unsafe or cannot be normalized."""


@dataclass(frozen=True, slots=True)
class _Member:
    name: str
    is_dir: bool
    executable: bool
    data: bytes


def _canonical_name(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw:
        raise SdistNormalizationError("sdist member name must be a non-empty text path")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise SdistNormalizationError(f"absolute sdist member path is forbidden: {raw!r}")
    parts = path.parts
    if any(part in {"", ".", ".."} for part in parts):
        raise SdistNormalizationError(f"non-canonical sdist member path is forbidden: {raw!r}")
    canonical = posixpath.normpath(raw)
    if canonical != raw.rstrip("/"):
        raise SdistNormalizationError(f"sdist member path is not canonical: {raw!r}")
    return canonical


def _read_members(source: Path) -> tuple[_Member, ...]:
    members: list[_Member] = []
    seen: set[str] = set()
    total_size = 0
    try:
        archive = tarfile.open(source, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError(f"cannot open sdist archive: {source}") from exc
    with archive:
        for index, info in enumerate(archive):
            if index >= _MAX_MEMBERS:
                raise SdistNormalizationError("sdist contains too many members")
            name = _canonical_name(info.name)
            if name in seen:
                raise SdistNormalizationError(f"duplicate sdist member: {name}")
            seen.add(name)
            if info.isdir():
                members.append(_Member(name=name, is_dir=True, executable=True, data=b""))
                continue
            if not info.isfile():
                raise SdistNormalizationError(f"unsupported sdist member type: {name}")
            if info.size < 0:
                raise SdistNormalizationError(f"negative sdist member size: {name}")
            total_size += info.size
            if total_size > _MAX_TOTAL_FILE_BYTES:
                raise SdistNormalizationError("sdist uncompressed payload exceeds the supported limit")
            extracted = archive.extractfile(info)
            if extracted is None:
                raise SdistNormalizationError(f"cannot read sdist member: {name}")
            data = extracted.read()
            if len(data) != info.size:
                raise SdistNormalizationError(f"truncated sdist member: {name}")
            members.append(
                _Member(
                    name=name,
                    is_dir=False,
                    executable=bool(info.mode & 0o111),
                    data=data,
                )
            )
    return tuple(sorted(members, key=lambda item: (item.name, not item.is_dir)))


def normalize_sdist(source: Path, destination: Path, *, epoch: int) -> Path:
    """Write a deterministic, path-hardened gzip tar archive atomically."""

    if type(epoch) is not int or epoch < 0:
        raise SdistNormalizationError("normalization epoch must be a non-negative integer")
    source = Path(source)
    destination = Path(destination)
    members = _read_members(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent
    )
    os.close(file_descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw:
            with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=epoch) as compressed:
                with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as output:
                    for member in members:
                        info = tarfile.TarInfo(member.name)
                        info.mtime = epoch
                        info.uid = 0
                        info.gid = 0
                        info.uname = ""
                        info.gname = ""
                        info.pax_headers = {}
                        if member.is_dir:
                            info.type = tarfile.DIRTYPE
                            info.mode = 0o755
                            info.size = 0
                            output.addfile(info)
                        else:
                            info.type = tarfile.REGTYPE
                            info.mode = 0o755 if member.executable else 0o644
                            info.size = len(member.data)
                            from io import BytesIO

                            output.addfile(info, BytesIO(member.data))
        os.chmod(temporary, 0o644)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--epoch", type=int, required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    normalize_sdist(args.source, args.destination, epoch=args.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
