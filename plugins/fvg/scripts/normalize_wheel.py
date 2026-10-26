from __future__ import annotations

import argparse
import base64
import csv
import hashlib
import io
import os
import posixpath
import stat
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

_MAX_MEMBERS = 10_000
_MAX_TOTAL_FILE_BYTES = 1 << 30
_ZIP_MIN_EPOCH = 315_532_800  # 1980-01-01 UTC.
_ZIP_MAX_EPOCH = 4_354_819_199  # 2107-12-31 23:59:59 UTC.


class WheelNormalizationError(ValueError):
    """Raised when a wheel is unsafe or cannot be normalized."""


@dataclass(frozen=True, slots=True)
class _Entry:
    name: str
    is_dir: bool
    executable: bool
    data: bytes


def _canonical_name(raw: str) -> str:
    if not isinstance(raw, str) or not raw or "\x00" in raw or "\\" in raw:
        raise WheelNormalizationError("wheel member name must be a canonical POSIX path")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise WheelNormalizationError(f"absolute wheel member path is forbidden: {raw!r}")
    if any(part in {"", ".", ".."} for part in path.parts):
        raise WheelNormalizationError(f"non-canonical wheel member path is forbidden: {raw!r}")
    canonical = posixpath.normpath(raw)
    if canonical != raw.rstrip("/"):
        raise WheelNormalizationError(f"wheel member path is not canonical: {raw!r}")
    return canonical


def _read_entries(source: Path) -> tuple[tuple[_Entry, ...], str]:
    try:
        archive = zipfile.ZipFile(source, mode="r")
    except (OSError, zipfile.BadZipFile) as exc:
        raise WheelNormalizationError(f"cannot open wheel archive: {source}") from exc
    entries: list[_Entry] = []
    seen: set[str] = set()
    record_names: list[str] = []
    total_size = 0
    with archive:
        infos = archive.infolist()
        if len(infos) > _MAX_MEMBERS:
            raise WheelNormalizationError("wheel contains too many members")
        for info in infos:
            name = _canonical_name(info.filename)
            if name in seen:
                raise WheelNormalizationError(f"duplicate wheel member: {name}")
            seen.add(name)
            if info.flag_bits & 0x1:
                raise WheelNormalizationError(f"encrypted wheel member is forbidden: {name}")
            mode = (info.external_attr >> 16) & 0xFFFF
            file_type = stat.S_IFMT(mode)
            is_dir = info.is_dir()
            if file_type == stat.S_IFLNK:
                raise WheelNormalizationError(f"symbolic link wheel member is forbidden: {name}")
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise WheelNormalizationError(f"unsupported wheel member type: {name}")
            if is_dir:
                entries.append(_Entry(name=name, is_dir=True, executable=True, data=b""))
                continue
            total_size += info.file_size
            if total_size > _MAX_TOTAL_FILE_BYTES:
                raise WheelNormalizationError("wheel uncompressed payload exceeds the supported limit")
            data = archive.read(info)
            if len(data) != info.file_size:
                raise WheelNormalizationError(f"truncated wheel member: {name}")
            if name.endswith(".dist-info/RECORD"):
                record_names.append(name)
                continue
            entries.append(
                _Entry(
                    name=name,
                    is_dir=False,
                    executable=bool(mode & 0o111),
                    data=data,
                )
            )
    if len(record_names) != 1:
        raise WheelNormalizationError("wheel must contain exactly one .dist-info/RECORD")
    record_name = record_names[0]
    if any(entry.name == record_name for entry in entries):
        raise WheelNormalizationError("wheel RECORD collision")
    return tuple(sorted(entries, key=lambda item: (item.name, not item.is_dir))), record_name


def _record(entries: tuple[_Entry, ...], record_name: str) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream, lineterminator="\n")
    for entry in entries:
        if entry.is_dir:
            continue
        digest = base64.urlsafe_b64encode(hashlib.sha256(entry.data).digest()).rstrip(b"=").decode("ascii")
        writer.writerow((entry.name, f"sha256={digest}", str(len(entry.data))))
    writer.writerow((record_name, "", ""))
    return stream.getvalue().encode("utf-8")


def _zip_timestamp(epoch: int) -> tuple[int, int, int, int, int, int]:
    if type(epoch) is not int or not _ZIP_MIN_EPOCH <= epoch <= _ZIP_MAX_EPOCH:
        raise WheelNormalizationError("wheel epoch must be a canonical integer in the ZIP timestamp range")
    value = datetime.fromtimestamp(epoch, tz=timezone.utc)
    # ZIP timestamps have two-second resolution.
    return (value.year, value.month, value.day, value.hour, value.minute, value.second - value.second % 2)


def normalize_wheel(source: Path, destination: Path, *, epoch: int) -> Path:
    timestamp = _zip_timestamp(epoch)
    entries, record_name = _read_entries(Path(source))
    record_data = _record(entries, record_name)
    complete = (*entries, _Entry(record_name, False, False, record_data))
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{destination.name}.", suffix=".tmp", dir=destination.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with zipfile.ZipFile(temporary, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for entry in sorted(complete, key=lambda item: (item.name, not item.is_dir)):
                filename = f"{entry.name}/" if entry.is_dir else entry.name
                info = zipfile.ZipInfo(filename=filename, date_time=timestamp)
                info.create_system = 3
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = (0o755 if entry.is_dir or entry.executable else 0o644) << 16
                if entry.is_dir:
                    info.external_attr |= 0x10
                archive.writestr(info, entry.data, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
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
    normalize_wheel(args.source, args.destination, epoch=args.epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
