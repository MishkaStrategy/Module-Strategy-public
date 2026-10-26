from __future__ import annotations

import argparse
import copy
import gzip
import io
import os
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

DEFAULT_SOURCE_DATE_EPOCH: Final = 946_684_800


class SdistNormalizationError(ValueError):
    """Raised when an sdist cannot be normalized safely."""


@dataclass(frozen=True, slots=True)
class _ArchiveRecord:
    name: str
    member: tarfile.TarInfo
    data: bytes | None


def _validate_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or ".." in path.parts:
        raise SdistNormalizationError(f"unsafe archive member path: {name!r}")
    if "\\" in name or (path.parts and path.parts[0].endswith(":")):
        raise SdistNormalizationError(f"non-portable archive member path: {name!r}")


def _read_records(source: Path) -> tuple[_ArchiveRecord, ...]:
    records: list[_ArchiveRecord] = []
    seen: set[str] = set()
    try:
        with tarfile.open(source, mode="r:gz") as archive:
            for member in archive.getmembers():
                _validate_member_name(member.name)
                if member.name in seen:
                    raise SdistNormalizationError(f"duplicate archive member path: {member.name!r}")
                seen.add(member.name)
                if not (member.isfile() or member.isdir()):
                    raise SdistNormalizationError(
                        f"unsupported archive member type: {member.name!r}"
                    )
                extracted = archive.extractfile(member) if member.isfile() else None
                data = extracted.read() if extracted is not None else None
                records.append(
                    _ArchiveRecord(
                        name=member.name,
                        member=copy.copy(member),
                        data=data,
                    )
                )
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError(f"cannot read sdist {source}") from exc
    return tuple(sorted(records, key=lambda record: record.name))


def normalize_sdist(
    source: Path,
    destination: Path | None = None,
    *,
    epoch: int = DEFAULT_SOURCE_DATE_EPOCH,
) -> Path:
    """Write a canonical gzip/tar representation of a trusted Python sdist."""

    source = source.resolve()
    destination = (destination or source).resolve()
    if epoch < 0:
        raise SdistNormalizationError("epoch must be non-negative")
    if not source.is_file():
        raise SdistNormalizationError(f"sdist does not exist: {source}")

    records = _read_records(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with temporary.open("wb") as raw_output:
            with gzip.GzipFile(
                filename="",
                mode="wb",
                fileobj=raw_output,
                compresslevel=9,
                mtime=epoch,
            ) as compressed_output:
                with tarfile.open(
                    fileobj=compressed_output,
                    mode="w",
                    format=tarfile.PAX_FORMAT,
                ) as normalized_archive:
                    for record in records:
                        member = copy.copy(record.member)
                        member.name = record.name
                        member.mtime = epoch
                        member.uid = 0
                        member.gid = 0
                        member.uname = ""
                        member.gname = ""
                        member.pax_headers = {}
                        normalized_archive.addfile(
                            member,
                            io.BytesIO(record.data) if record.data is not None else None,
                        )
        os.replace(temporary, destination)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Normalize a Python sdist deterministically")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", nargs="?", type=Path)
    parser.add_argument("--epoch", type=int, default=DEFAULT_SOURCE_DATE_EPOCH)
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    normalize_sdist(
        arguments.source,
        arguments.destination,
        epoch=arguments.epoch,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
