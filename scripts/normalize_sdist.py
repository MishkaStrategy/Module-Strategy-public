from __future__ import annotations

import argparse
import copy
import gzip
import io
import os
import tarfile
import tempfile
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

DEFAULT_SOURCE_DATE_EPOCH = 946_684_800
MAX_MEMBERS = 100_000
MAX_TOTAL_FILE_BYTES = 512 * 1024 * 1024


class SdistNormalizationError(ValueError):
    """Raised when an sdist cannot be normalized safely."""


def _validated_epoch(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("epoch must be an integer")
    if value < 0:
        raise SdistNormalizationError(
            "SOURCE_DATE_EPOCH cannot be negative; it must be non-negative"
        )
    return value


def _validate_member_name(name: str) -> None:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts:
        raise SdistNormalizationError(f"unsafe sdist member path: {name!r}")
    normalized = path.as_posix()
    if normalized != name.rstrip("/") and not (
        name.endswith("/") and normalized == name.rstrip("/")
    ):
        raise SdistNormalizationError(f"non-canonical sdist member path: {name!r}")


def _normalized_member(member: tarfile.TarInfo, epoch: int) -> tarfile.TarInfo:
    normalized = copy.copy(member)
    normalized.mtime = epoch
    normalized.uid = 0
    normalized.gid = 0
    normalized.uname = ""
    normalized.gname = ""
    normalized.pax_headers = {
        key: value
        for key, value in sorted(member.pax_headers.items())
        if key not in {"atime", "ctime", "mtime", "uid", "gid", "uname", "gname"}
    }
    return normalized


def normalized_sdist_bytes(source: Path, *, epoch: int) -> bytes:
    """Return deterministic gzip-compressed tar bytes for an existing sdist."""

    epoch = _validated_epoch(epoch)
    if source.is_symlink() or not source.is_file():
        raise SdistNormalizationError(f"sdist must be a regular file: {source}")
    try:
        archive = tarfile.open(source, mode="r:gz")
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError(f"invalid gzip-compressed sdist: {source}") from exc

    tar_buffer = io.BytesIO()
    try:
        with (
            archive,
            tarfile.open(
                fileobj=tar_buffer,
                mode="w",
                format=tarfile.PAX_FORMAT,
            ) as output,
        ):
            members = archive.getmembers()
            if len(members) > MAX_MEMBERS:
                raise SdistNormalizationError("sdist exceeds member limit")
            names: set[str] = set()
            total_file_bytes = 0
            for member in members:
                _validate_member_name(member.name)
                if not member.isdir() and not member.isfile():
                    raise SdistNormalizationError(
                        f"unsupported sdist member type for {member.name!r}"
                    )
                if member.name in names:
                    raise SdistNormalizationError(f"duplicate sdist member path: {member.name!r}")
                names.add(member.name)
                if member.isfile():
                    total_file_bytes += member.size
                    if total_file_bytes > MAX_TOTAL_FILE_BYTES:
                        raise SdistNormalizationError("sdist exceeds uncompressed size limit")

            for member in sorted(members, key=lambda item: item.name):
                normalized = _normalized_member(member, epoch)
                payload = archive.extractfile(member) if member.isfile() else None
                output.addfile(normalized, payload)
    except SdistNormalizationError:
        raise
    except (OSError, tarfile.TarError) as exc:
        raise SdistNormalizationError(f"failed to normalize sdist: {source}") from exc

    compressed = io.BytesIO()
    with gzip.GzipFile(
        filename="",
        mode="wb",
        fileobj=compressed,
        compresslevel=9,
        mtime=epoch,
    ) as stream:
        stream.write(tar_buffer.getvalue())
    return compressed.getvalue()


def normalize_sdist(
    source: Path,
    destination: Path | None = None,
    *,
    epoch: int,
) -> Path:
    """Write a deterministic sdist atomically and return its destination path."""

    if source.is_symlink() or not source.is_file():
        raise SdistNormalizationError(f"sdist must be a regular file: {source}")
    source = source.resolve()
    destination = source if destination is None else destination.resolve()
    payload = normalized_sdist_bytes(source, epoch=epoch)
    destination.parent.mkdir(parents=True, exist_ok=True)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o644)
        os.replace(temporary, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Normalize one or more .tar.gz Python sdists reproducibly."
    )
    parser.add_argument("sources", nargs="+", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        help="Destination path; valid only when normalizing one source.",
    )
    parser.add_argument(
        "--epoch",
        type=int,
        default=None,
        help="Unix timestamp. Defaults to SOURCE_DATE_EPOCH or the canonical epoch.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.output is not None and len(args.sources) != 1:
        raise SystemExit("--output requires exactly one source")
    raw_epoch = args.epoch
    if raw_epoch is None:
        environment_epoch = os.environ.get("SOURCE_DATE_EPOCH")
        try:
            raw_epoch = (
                DEFAULT_SOURCE_DATE_EPOCH if environment_epoch is None else int(environment_epoch)
            )
        except ValueError as exc:
            raise SystemExit("SOURCE_DATE_EPOCH must be an integer") from exc

    for source in args.sources:
        destination = args.output if args.output is not None else source
        normalize_sdist(source, destination, epoch=raw_epoch)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
