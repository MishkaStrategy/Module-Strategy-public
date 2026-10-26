#!/usr/bin/env python3
"""Bounded, deterministic GitHub job-output transport for sanitized release evidence."""

from __future__ import annotations

import argparse
import base64
import binascii
import gzip
import hashlib
import io
import os
import stat
import tarfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Iterable

# GitHub documents a 1 MiB per-job output limit, approximated as UTF-16.
# Keep the ASCII base64 payload below 48k characters. This is ample for the
# small sanitized JSON evidence bundle and stays far below the job-output bound.
MAX_JOB_OUTPUT_CHARS = 48_000
MAX_DECOMPRESSED_TAR_BYTES = 20 * 1024 * 1024
MAX_REGULAR_FILES = 256
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_FILE_BYTES = 16 * 1024 * 1024
MAX_SOURCE_ENTRIES = 4096
_NO_FOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_CLOEXEC = getattr(os, "O_CLOEXEC", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_TAR_EXTENSION_TYPES = {b"x", b"g", b"L", b"K", b"S"}


def _canonical_archive_name(name: str) -> str:
    if (
        not name
        or "\\" in name
        or "\x00" in name
        or any(ord(character) < 0x20 or ord(character) == 0x7F for character in name)
    ):
        raise ValueError(f"unsafe evidence archive entry: {name!r}")
    pure = PurePosixPath(name)
    if (
        pure.is_absolute()
        or pure.as_posix() != name
        or not pure.parts
        or any(part in {"", ".", ".."} for part in pure.parts)
        or ":" in pure.parts[0]
    ):
        raise ValueError(f"unsafe evidence archive entry: {name!r}")
    return name


def _validate_archive_name_set(names: Iterable[str], *, label: str) -> None:
    seen: set[str] = set()
    for name in names:
        _canonical_archive_name(name)
        folded = name.casefold()
        if folded in seen:
            raise ValueError(f"duplicate {label} entry: {name}")
        parents = {
            "/".join(folded.split("/")[:index]) for index in range(1, len(folded.split("/")))
        }
        if parents & seen or any(existing.startswith(f"{folded}/") for existing in seen):
            raise ValueError(f"conflicting {label} entry: {name}")
        seen.add(folded)


def _require_safe_open_support() -> None:
    if not _NO_FOLLOW or not _DIRECTORY or os.open not in os.supports_dir_fd:
        raise ValueError("platform cannot safely open evidence files without following symlinks")


def _read_open_file(
    descriptor: int, expected_stat: os.stat_result, name: str, allowance: int
) -> bytes:
    try:
        actual = os.fstat(descriptor)
        if not stat.S_ISREG(actual.st_mode) or (actual.st_dev, actual.st_ino) != (
            expected_stat.st_dev,
            expected_stat.st_ino,
        ):
            raise ValueError(f"evidence file changed during safe open: {name}")
        if actual.st_size > MAX_FILE_BYTES:
            raise ValueError(f"evidence file exceeds maximum size: {name}")
        if actual.st_size > allowance:
            raise ValueError("evidence archive exceeds maximum expanded file bytes")
        maximum = min(MAX_FILE_BYTES, allowance)
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(64 * 1024, maximum + 1 - total))
            if not chunk:
                break
            total += len(chunk)
            if total > MAX_FILE_BYTES:
                raise ValueError(f"evidence file exceeds maximum size: {name}")
            if total > allowance:
                raise ValueError("evidence archive exceeds maximum expanded file bytes")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _open_regular_entry(
    parent_fd: int, entry: os.DirEntry[str], name: str, allowance: int
) -> bytes:
    if entry.is_symlink():
        raise ValueError(f"symlink evidence entry is forbidden: {name}")
    expected_stat = entry.stat(follow_symlinks=False)
    if not stat.S_ISREG(expected_stat.st_mode):
        raise ValueError(f"evidence entry changed type during enumeration: {name}")
    if expected_stat.st_size > MAX_FILE_BYTES:
        raise ValueError(f"evidence file exceeds maximum size: {name}")
    if expected_stat.st_size > allowance:
        raise ValueError("evidence archive exceeds maximum expanded file bytes")
    try:
        descriptor = os.open(
            entry.name,
            os.O_RDONLY | _NO_FOLLOW | _CLOEXEC | _NONBLOCK,
            dir_fd=parent_fd,
        )
    except OSError as exc:
        raise ValueError(f"unable to safely open evidence file: {name}") from exc
    return _read_open_file(descriptor, expected_stat, name, allowance)


def _collect_source_files(
    source: Path,
    root_fd: int,
    root_stat: os.stat_result,
    *,
    archive_prefix: str,
    files: list[tuple[str, bytes]],
    entries_seen: list[int],
) -> None:
    if stat.S_ISREG(root_stat.st_mode):
        if len(files) >= MAX_REGULAR_FILES:
            os.close(root_fd)
            raise ValueError("evidence archive exceeds maximum regular-file count")
        try:
            name = _canonical_archive_name(
                f"{archive_prefix}/{source.name}" if archive_prefix else source.name
            )
        except Exception:
            os.close(root_fd)
            raise
        allowance = MAX_TOTAL_FILE_BYTES - sum(len(data) for _, data in files)
        payload = _read_open_file(root_fd, root_stat, name, allowance)
        files.append((name, payload))
        return
    if not stat.S_ISDIR(root_stat.st_mode):
        os.close(root_fd)
        raise ValueError(f"evidence source is not a regular file or directory: {source}")

    stack: list[tuple[int, tuple[str, ...]]] = [(root_fd, ())]
    try:
        while stack:
            directory_fd, parent_parts = stack.pop()
            try:
                with os.scandir(directory_fd) as scan:
                    for entry in scan:
                        entries_seen[0] += 1
                        if entries_seen[0] > MAX_SOURCE_ENTRIES:
                            raise ValueError(
                                "evidence source exceeds maximum enumerated entry count"
                            )
                        parts = (*parent_parts, entry.name)
                        relative_name = "/".join(parts)
                        archive_name = _canonical_archive_name(
                            f"{archive_prefix}/{relative_name}" if archive_prefix else relative_name
                        )
                        if entry.is_symlink():
                            raise ValueError(f"symlink evidence entry is forbidden: {archive_name}")
                        if entry.is_dir(follow_symlinks=False):
                            expected_stat = entry.stat(follow_symlinks=False)
                            try:
                                child_fd = os.open(
                                    entry.name,
                                    os.O_RDONLY | _DIRECTORY | _NO_FOLLOW | _CLOEXEC | _NONBLOCK,
                                    dir_fd=directory_fd,
                                )
                            except OSError as exc:
                                raise ValueError(
                                    f"unable to safely open evidence directory: {archive_name}"
                                ) from exc
                            actual = os.fstat(child_fd)
                            if not stat.S_ISDIR(actual.st_mode) or (
                                actual.st_dev,
                                actual.st_ino,
                            ) != (
                                expected_stat.st_dev,
                                expected_stat.st_ino,
                            ):
                                os.close(child_fd)
                                raise ValueError(
                                    f"evidence directory changed during safe open: {archive_name}"
                                )
                            stack.append((child_fd, parts))
                        elif entry.is_file(follow_symlinks=False):
                            if len(files) >= MAX_REGULAR_FILES:
                                raise ValueError(
                                    "evidence archive exceeds maximum regular-file count"
                                )
                            allowance = MAX_TOTAL_FILE_BYTES - sum(len(data) for _, data in files)
                            payload = _open_regular_entry(
                                directory_fd, entry, archive_name, allowance
                            )
                            files.append((archive_name, payload))
            finally:
                os.close(directory_fd)
    finally:
        for descriptor, _parts in stack:
            try:
                os.close(descriptor)
            except OSError:
                pass


def build_archive(sources: Iterable[Path]) -> bytes:
    source_list: list[Path] = []
    for source in sources:
        if len(source_list) >= MAX_REGULAR_FILES:
            raise ValueError("evidence sources exceed maximum source count")
        source_list.append(Path(source))
    if not source_list:
        raise ValueError("no evidence sources supplied")

    _require_safe_open_support()
    roots: list[tuple[Path, int, os.stat_result]] = []
    files: list[tuple[str, bytes]] = []
    try:
        for source in source_list:
            try:
                descriptor = os.open(source, os.O_RDONLY | _NO_FOLLOW | _CLOEXEC | _NONBLOCK)
            except OSError as exc:
                raise ValueError(f"unable to safely open evidence source: {source}") from exc
            root_stat = os.fstat(descriptor)
            if not (stat.S_ISREG(root_stat.st_mode) or stat.S_ISDIR(root_stat.st_mode)):
                os.close(descriptor)
                raise ValueError(f"evidence source is not a regular file or directory: {source}")
            roots.append((source, descriptor, root_stat))

        flatten_single_directory = len(roots) == 1 and stat.S_ISDIR(roots[0][2].st_mode)
        entries_seen = [0]
        for index, (source, descriptor, root_stat) in enumerate(roots):
            archive_prefix = (
                source.name
                if stat.S_ISDIR(root_stat.st_mode) and not flatten_single_directory
                else ""
            )
            _collect_source_files(
                source,
                descriptor,
                root_stat,
                archive_prefix=archive_prefix,
                files=files,
                entries_seen=entries_seen,
            )
            # _collect_source_files consumes and closes each root descriptor.
            roots[index] = (source, -1, root_stat)
    finally:
        for _source, descriptor, _root_stat in roots:
            if descriptor < 0:
                continue
            try:
                os.close(descriptor)
            except OSError:
                pass

    if not files:
        raise ValueError("evidence sources contain no files")
    files.sort(key=lambda item: item[0])
    _validate_archive_name_set((name for name, _data in files), label="evidence archive")
    tar_buffer = io.BytesIO()
    with tarfile.open(fileobj=tar_buffer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, payload in files:
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            info.mode = 0o644
            info.mtime = 0
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            archive.addfile(info, io.BytesIO(payload))
    tar_bytes = tar_buffer.getvalue()
    if len(tar_bytes) > MAX_DECOMPRESSED_TAR_BYTES:
        raise ValueError("evidence archive exceeds maximum decompressed tar bytes")
    raw = io.BytesIO()
    with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0, filename="") as compressed:
        compressed.write(tar_bytes)
    return raw.getvalue()


def encode_archive(archive: bytes) -> tuple[str, str]:
    payload = base64.b64encode(archive).decode("ascii")
    if len(payload) > MAX_JOB_OUTPUT_CHARS:
        raise ValueError(
            "sanitized evidence handoff exceeds bounded GitHub job-output capacity: "
            f"{len(payload)} > {MAX_JOB_OUTPUT_CHARS} characters"
        )
    return payload, hashlib.sha256(archive).hexdigest()


def decode_archive(payload: str, expected_sha256: str) -> bytes:
    if len(payload) > MAX_JOB_OUTPUT_CHARS:
        raise ValueError("evidence handoff payload exceeds bounded capacity")
    if len(expected_sha256) != 64 or any(ch not in "0123456789abcdef" for ch in expected_sha256):
        raise ValueError("expected SHA-256 must be lowercase hexadecimal")
    try:
        archive = base64.b64decode(payload.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ValueError("evidence handoff payload is not valid base64") from exc
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected_sha256:
        raise ValueError(
            f"evidence handoff SHA-256 mismatch: expected={expected_sha256} actual={actual}"
        )
    try:
        tar_bytes = _decompress_bounded_tar(archive)
        _validate_tar_members(tar_bytes)
    except tarfile.TarError as exc:
        raise ValueError("evidence handoff is not a valid deterministic tar.gz archive") from exc
    return archive


def _decompress_bounded_tar(archive: bytes) -> bytes:
    chunks: list[bytes] = []
    total = 0
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(archive), mode="rb") as compressed:
            while True:
                remaining = MAX_DECOMPRESSED_TAR_BYTES + 1 - total
                chunk = compressed.read(min(64 * 1024, remaining))
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DECOMPRESSED_TAR_BYTES:
                    raise ValueError("evidence handoff exceeds maximum decompressed tar bytes")
                chunks.append(chunk)
    except (OSError, EOFError, zlib.error) as exc:
        raise ValueError("evidence handoff is not a valid gzip stream") from exc
    return b"".join(chunks)


def _reject_tar_extension_headers(tar_bytes: bytes) -> None:
    """Reject extensions before tarfile can recursively process their metadata."""
    offset = 0
    while offset < len(tar_bytes):
        if offset + 512 > len(tar_bytes):
            if any(tar_bytes[offset:]):
                raise tarfile.ReadError("truncated tar header block")
            return
        header = tar_bytes[offset : offset + 512]
        if not any(header):
            return
        typeflag = header[156:157]
        if typeflag in _TAR_EXTENSION_TYPES:
            raise ValueError("unsupported evidence handoff tar extension header")
        size_field = header[124:136]
        if size_field and size_field[0] & 0x80:
            raise ValueError("unsupported base-256 tar size field")
        raw_size = size_field.strip(b"\0 ")
        if raw_size and any(byte < ord("0") or byte > ord("7") for byte in raw_size):
            raise tarfile.ReadError("invalid tar member size field")
        size = int(raw_size or b"0", 8)
        next_offset = offset + 512 + ((size + 511) // 512) * 512
        if next_offset > len(tar_bytes):
            raise tarfile.ReadError("truncated tar member body")
        offset = next_offset


def _validate_tar_members(tar_bytes: bytes) -> None:
    _reject_tar_extension_headers(tar_bytes)
    try:
        with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as tar:
            count = 0
            total_file_bytes = 0
            names: list[str] = []
            for member in tar:
                count += 1
                if count > MAX_REGULAR_FILES:
                    raise ValueError("evidence handoff exceeds maximum regular-file count")
                name = _canonical_archive_name(member.name)
                if member.type not in {tarfile.REGTYPE, tarfile.AREGTYPE} or member.sparse:
                    raise ValueError(f"unsupported evidence handoff entry: {name}")
                names.append(name)
                if not isinstance(member.size, int) or member.size < 0:
                    raise ValueError(f"invalid evidence handoff file size: {name}")
                if member.size > MAX_FILE_BYTES:
                    raise ValueError(f"evidence handoff file exceeds maximum size: {name}")
                total_file_bytes += member.size
                if total_file_bytes > MAX_TOTAL_FILE_BYTES:
                    raise ValueError("evidence handoff exceeds maximum expanded file bytes")
                file_obj = tar.extractfile(member)
                if file_obj is None:
                    raise ValueError(f"unable to read evidence handoff file: {name}")
                remaining = member.size
                while remaining:
                    chunk = file_obj.read(min(64 * 1024, remaining))
                    if not chunk:
                        raise ValueError(f"truncated evidence handoff file: {name}")
                    remaining -= len(chunk)
            if not names:
                raise ValueError("evidence handoff archive is empty")
            _validate_archive_name_set(names, label="evidence handoff")
    except tarfile.TarError as exc:
        raise ValueError("evidence handoff is not a valid deterministic tar.gz archive") from exc


def _write_github_output(name: str, value: str) -> None:
    output_path = os.environ.get("GITHUB_OUTPUT")
    if not output_path:
        raise ValueError("GITHUB_OUTPUT is required")
    with Path(output_path).open("a", encoding="utf-8") as handle:
        handle.write(f"{name}={value}\n")


def _pack_output() -> int:
    raw_sources = os.environ.get("SOURCE_PATHS", "")
    sources = [Path(value) for value in raw_sources.splitlines() if value.strip()]
    archive = build_archive(sources)
    payload, digest = encode_archive(archive)
    _write_github_output("payload", payload)
    _write_github_output("sha256", digest)
    _write_github_output("encoded_chars", str(len(payload)))
    return 0


def _unpack_input(output: Path) -> int:
    payload = os.environ.get("HANDOFF_PAYLOAD", "")
    expected = os.environ.get("HANDOFF_SHA256", "")
    archive = decode_archive(payload, expected)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(archive)
    output.chmod(0o600)
    return 0


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("pack-output")
    unpack = subparsers.add_parser("unpack-input")
    unpack.add_argument("--output", type=Path, required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.command == "pack-output":
        return _pack_output()
    if args.command == "unpack-input":
        return _unpack_input(args.output)
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
