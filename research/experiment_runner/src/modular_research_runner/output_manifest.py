from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from .canonical import CanonicalizationError, canonical_json_bytes, canonical_json_loads

if TYPE_CHECKING:
    from .engine import BacktestRunReference

MANIFEST_NAME = "research-runner-output-manifest.json"
MANIFEST_SCHEMA_VERSION = 1
MAX_ARTIFACTS = 100_000
MAX_ARTIFACT_BYTES = 8 * 1024 * 1024 * 1024
MAX_TOTAL_ARTIFACT_BYTES = 64 * 1024 * 1024 * 1024
MAX_MANIFEST_BYTES = 16 * 1024 * 1024
MAX_RELATIVE_PATH_LENGTH = 2048
_READ_CHUNK_BYTES = 1024 * 1024


class OutputManifestError(RuntimeError):
    """Raised when Backtest Engine output cannot be bound to a safe manifest."""


def _safe_relative_path(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or len(value) > MAX_RELATIVE_PATH_LENGTH:
        raise OutputManifestError("artifact path is invalid")
    if "\\" in value:
        raise OutputManifestError("artifact path is invalid")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise OutputManifestError("artifact path is invalid")
    return path


def _output_root(result: BacktestRunReference) -> Path:
    root = result.output_directory
    if root.is_symlink() or not root.is_dir():
        raise OutputManifestError("output directory must be a real directory")
    return root.resolve(strict=True)


def output_manifest_path(result: BacktestRunReference) -> Path:
    """Return the sidecar path without placing runner metadata in Engine output."""

    root = _output_root(result)
    identity = hashlib.sha256(
        canonical_json_bytes(
            {
                "requested_run_id": result.requested_run_id,
                "engine_run_id": result.engine_run_id,
                "output_directory": root,
            }
        )
    ).hexdigest()
    return root.parent / f".{MANIFEST_NAME}.{identity}"


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int, int]:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _open_regular_file(
    path: Path,
    *,
    maximum_bytes: int,
    subject: str,
) -> tuple[int, os.stat_result]:
    try:
        before = path.lstat()
    except OSError as exc:
        raise OutputManifestError(f"{subject} cannot be inspected") from exc
    if not stat.S_ISREG(before.st_mode):
        raise OutputManifestError(f"{subject} must be a regular file")
    if before.st_size > maximum_bytes:
        raise OutputManifestError(f"{subject} exceeds its size limit")

    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OutputManifestError(f"{subject} cannot be opened safely") from exc

    try:
        opened = os.fstat(descriptor)
    except OSError as exc:
        os.close(descriptor)
        raise OutputManifestError(f"{subject} cannot be inspected safely") from exc
    if not stat.S_ISREG(opened.st_mode) or (
        before.st_dev,
        before.st_ino,
    ) != (
        opened.st_dev,
        opened.st_ino,
    ):
        os.close(descriptor)
        raise OutputManifestError(f"{subject} changed during verification")
    if opened.st_size > maximum_bytes:
        os.close(descriptor)
        raise OutputManifestError(f"{subject} exceeds its size limit")
    return descriptor, opened


def _regular_file_metadata(path: Path) -> tuple[int, str]:
    """Hash one unchanged regular artifact through a single descriptor."""

    descriptor, opened = _open_regular_file(
        path,
        maximum_bytes=MAX_ARTIFACT_BYTES,
        subject="output artifact",
    )
    try:
        digest = hashlib.sha256()
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > MAX_ARTIFACT_BYTES:
                raise OutputManifestError("output artifact exceeds its size limit")
            digest.update(chunk)

        after = os.fstat(descriptor)
        if bytes_read != opened.st_size or _stat_identity(after) != _stat_identity(opened):
            raise OutputManifestError("output artifact changed during verification")
        return opened.st_size, digest.hexdigest()
    finally:
        os.close(descriptor)


def _read_manifest_bytes(path: Path) -> bytes:
    descriptor, opened = _open_regular_file(
        path,
        maximum_bytes=MAX_MANIFEST_BYTES,
        subject="output manifest",
    )
    try:
        chunks: list[bytes] = []
        bytes_read = 0
        while True:
            chunk = os.read(descriptor, _READ_CHUNK_BYTES)
            if not chunk:
                break
            bytes_read += len(chunk)
            if bytes_read > MAX_MANIFEST_BYTES:
                raise OutputManifestError("output manifest exceeds its size limit")
            chunks.append(chunk)

        after = os.fstat(descriptor)
        if bytes_read != opened.st_size or _stat_identity(after) != _stat_identity(opened):
            raise OutputManifestError("output manifest changed during verification")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _artifact_rows(root: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    total_size = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            raise OutputManifestError("output artifacts must not be symlinks")
        if path.is_dir():
            continue
        _safe_relative_path(relative)
        size, digest = _regular_file_metadata(path)
        total_size += size
        if total_size > MAX_TOTAL_ARTIFACT_BYTES:
            raise OutputManifestError("output artifacts exceed their total size limit")
        rows.append(
            {
                "path": relative,
                "size_bytes": size,
                "sha256": digest,
            }
        )
        if len(rows) > MAX_ARTIFACTS:
            raise OutputManifestError("output contains too many artifacts")
    return rows


def write_output_manifest(result: BacktestRunReference) -> Path:
    """Atomically bind an engine result to the exact files in its output directory."""

    root = _output_root(result)
    payload = canonical_json_bytes(
        {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "requested_run_id": result.requested_run_id,
            "engine_run_id": result.engine_run_id,
            "output_directory": root,
            "bars_processed": result.bars_processed,
            "restart_verified": result.restart_verified,
            "artifacts": _artifact_rows(root),
        }
    )
    if len(payload) > MAX_MANIFEST_BYTES:
        raise OutputManifestError("output manifest exceeds its size limit")
    target = output_manifest_path(result)
    if target.is_symlink():
        raise OutputManifestError("output manifest must not be a symlink")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{MANIFEST_NAME}.",
        dir=root.parent,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(root.parent, os.O_RDONLY)
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], *, subject: str) -> None:
    if set(value) != expected:
        raise OutputManifestError(f"{subject} has an invalid schema")


def read_output_manifest(result: BacktestRunReference) -> Mapping[str, Any]:
    root = _output_root(result)
    manifest = output_manifest_path(result)
    try:
        raw = canonical_json_loads(_read_manifest_bytes(manifest))
    except CanonicalizationError as exc:
        raise OutputManifestError("output manifest cannot be decoded") from exc
    if not isinstance(raw, Mapping):
        raise OutputManifestError("output manifest must be an object")
    _require_exact_keys(
        raw,
        {
            "schema_version",
            "requested_run_id",
            "engine_run_id",
            "output_directory",
            "bars_processed",
            "restart_verified",
            "artifacts",
        },
        subject="output manifest",
    )
    if raw["schema_version"] != MANIFEST_SCHEMA_VERSION:
        raise OutputManifestError("output manifest schema version is unsupported")
    if raw["requested_run_id"] != result.requested_run_id:
        raise OutputManifestError("output manifest run ID does not match")
    if raw["engine_run_id"] != result.engine_run_id:
        raise OutputManifestError("output manifest engine run ID does not match")
    if raw["output_directory"] != root:
        raise OutputManifestError("output manifest directory does not match")
    if raw["bars_processed"] != result.bars_processed:
        raise OutputManifestError("output manifest bar count does not match")
    if raw["restart_verified"] is not result.restart_verified:
        raise OutputManifestError("output manifest restart status does not match")
    artifacts = raw["artifacts"]
    if (
        not isinstance(artifacts, Sequence)
        or isinstance(artifacts, str | bytes | bytearray)
        or len(artifacts) > MAX_ARTIFACTS
    ):
        raise OutputManifestError("output manifest artifacts are invalid")

    expected: dict[str, tuple[int, str]] = {}
    total_size = 0
    for artifact in artifacts:
        if not isinstance(artifact, Mapping):
            raise OutputManifestError("output manifest artifact is invalid")
        _require_exact_keys(
            artifact,
            {"path", "size_bytes", "sha256"},
            subject="output manifest artifact",
        )
        relative = _safe_relative_path(artifact["path"])
        relative_text = relative.as_posix()
        if relative_text in expected:
            raise OutputManifestError("output manifest contains duplicate artifacts")
        size = artifact["size_bytes"]
        digest = artifact["sha256"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise OutputManifestError("output manifest artifact size is invalid")
        if size > MAX_ARTIFACT_BYTES:
            raise OutputManifestError("output manifest artifact exceeds its size limit")
        total_size += size
        if total_size > MAX_TOTAL_ARTIFACT_BYTES:
            raise OutputManifestError("output manifest artifacts exceed their total size limit")
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise OutputManifestError("output manifest artifact digest is invalid")
        expected[relative_text] = (size, digest)

    observed = {row["path"]: (row["size_bytes"], row["sha256"]) for row in _artifact_rows(root)}
    if observed != expected:
        raise OutputManifestError("output directory does not match its manifest")
    return raw


def verify_output_manifest(result: BacktestRunReference) -> bool:
    try:
        read_output_manifest(result)
    except (OutputManifestError, OSError):
        return False
    return True
