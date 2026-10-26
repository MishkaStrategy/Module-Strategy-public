from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .manifest import (
    RecoveryEntry,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    _identifier,
    manifest_from_payload,
)
from .snapshot import (
    SnapshotSpec,
    _fsync_directory,
    _safe_entry,
    _safe_root,
    snapshot_entry,
    verify_snapshot_file,
)

_MANIFEST_NAME = "recovery-manifest.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_MAX_MANIFEST_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class SnapshotRequest:
    source_root: Path
    spec: SnapshotSpec


@dataclass(frozen=True, slots=True)
class BackupPackage:
    manifest: RecoveryManifest
    manifest_path: Path
    manifest_sha256: str


def create_backup_package(
    *,
    backup_root: Path,
    source_commit: str,
    platform_release_id: str,
    requests: tuple[SnapshotRequest, ...],
) -> BackupPackage:
    """Create one closed recovery package and publish its canonical manifest last."""

    safe_backup = _safe_root(backup_root, "backup root")
    _require_empty_package_root(safe_backup)
    ordered = _preflight_requests(
        requests,
        source_commit=source_commit,
        platform_release_id=platform_release_id,
    )

    entries: list[RecoveryEntry] = []
    published_payloads: list[Path] = []
    try:
        for request in ordered:
            entry = snapshot_entry(
                source_root=request.source_root,
                backup_root=safe_backup,
                spec=request.spec,
            )
            entries.append(entry)
            if entry.snapshot_kind is not SnapshotKind.REFERENCE:
                payload = safe_backup / "payload" / entry.logical_id
                published_payloads.append(payload)
                verify_snapshot_file(safe_backup, entry)

        manifest = RecoveryManifest(
            source_commit=source_commit,
            platform_release_id=platform_release_id,
            entries=tuple(entries),
        )
        _verify_payload_inventory(safe_backup, manifest)
        manifest_path = _publish_manifest(safe_backup, manifest.canonical_bytes())
        return BackupPackage(
            manifest=manifest,
            manifest_path=manifest_path,
            manifest_sha256=manifest.sha256(),
        )
    except Exception:
        _cleanup_current_attempt(safe_backup, published_payloads)
        raise


def load_backup_manifest(*, backup_root: Path, expected_manifest_sha256: str) -> RecoveryManifest:
    """Load a closed package only when its canonical manifest matches trusted evidence."""

    if _SHA256.fullmatch(expected_manifest_sha256) is None:
        raise RecoveryManifestError("expected manifest SHA-256 must be lowercase hex")
    safe_backup = _safe_root(backup_root, "backup root")
    raw = _read_manifest(safe_backup)
    if hashlib.sha256(raw).hexdigest() != expected_manifest_sha256:
        raise RecoveryManifestError("recovery manifest checksum mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RecoveryManifestError("recovery manifest is not valid canonical JSON") from exc
    if not isinstance(payload, dict):
        raise RecoveryManifestError("recovery manifest must be a JSON object")
    manifest = manifest_from_payload(payload)
    if raw != manifest.canonical_bytes():
        raise RecoveryManifestError("recovery manifest bytes are not canonical")
    _verify_closed_package(safe_backup, manifest)
    return manifest


def _preflight_requests(
    requests: tuple[SnapshotRequest, ...],
    *,
    source_commit: str,
    platform_release_id: str,
) -> tuple[SnapshotRequest, ...]:
    if _SOURCE_COMMIT.fullmatch(source_commit) is None:
        raise RecoveryManifestError("source_commit must be a full lowercase Git SHA")
    _identifier(platform_release_id, "platform_release_id")
    if not requests:
        raise RecoveryManifestError("backup package requests must not be empty")

    ordered = tuple(sorted(requests, key=lambda item: item.spec.logical_id))
    logical_ids = [item.spec.logical_id for item in ordered]
    if len(logical_ids) != len(set(logical_ids)):
        raise RecoveryManifestError("backup package logical_id values must be unique")
    targets = [
        item.spec.restore_target
        for item in ordered
        if item.spec.snapshot_kind is not SnapshotKind.REFERENCE
        and item.spec.restore_target is not None
    ]
    if len(targets) != len(set(targets)):
        raise RecoveryManifestError("backup package restore_target values must be unique")

    normalized: list[SnapshotRequest] = []
    for request in ordered:
        safe_source = _safe_root(request.source_root, "source root")
        _safe_entry(safe_source, request.spec.source, "source")
        normalized.append(SnapshotRequest(source_root=safe_source, spec=request.spec))
    return tuple(normalized)


def _require_empty_package_root(root: Path) -> None:
    try:
        if next(root.iterdir(), None) is not None:
            raise RecoveryManifestError("backup package root must be empty")
    except OSError as exc:
        raise RecoveryManifestError("backup package root cannot be inspected safely") from exc


def _verify_payload_inventory(root: Path, manifest: RecoveryManifest) -> None:
    expected = {
        entry.logical_id
        for entry in manifest.entries
        if entry.snapshot_kind is not SnapshotKind.REFERENCE
    }
    payload = root / "payload"
    if not expected:
        if payload.exists() or payload.is_symlink():
            raise RecoveryManifestError("reference-only package must not contain payload bytes")
        return
    if payload.is_symlink() or not payload.is_dir():
        raise RecoveryManifestError("backup payload root must be a real directory")
    try:
        actual = {item.name for item in payload.iterdir()}
    except OSError as exc:
        raise RecoveryManifestError("backup payload inventory cannot be inspected safely") from exc
    if actual != expected:
        raise RecoveryManifestError("backup payload inventory differs from manifest")
    for entry in manifest.entries:
        verify_snapshot_file(root, entry)


def _verify_closed_package(root: Path, manifest: RecoveryManifest) -> None:
    expected_payloads = {
        entry.logical_id
        for entry in manifest.entries
        if entry.snapshot_kind is not SnapshotKind.REFERENCE
    }
    expected_root = {_MANIFEST_NAME}
    if expected_payloads:
        expected_root.add("payload")
    try:
        actual_root = {item.name for item in root.iterdir()}
    except OSError as exc:
        raise RecoveryManifestError("backup package inventory cannot be inspected safely") from exc
    if actual_root != expected_root:
        raise RecoveryManifestError("backup package contains undeclared files")
    _verify_payload_inventory(root, manifest)


def _publish_manifest(root: Path, data: bytes) -> Path:
    target = root / _MANIFEST_NAME
    if target.exists() or target.is_symlink():
        raise RecoveryManifestError("recovery manifest destination already exists")

    descriptor = -1
    temporary: Path | None = None
    published = False
    identity: tuple[int, int] | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".recovery-manifest.",
            suffix=".tmp",
            dir=root,
        )
        temporary = Path(temporary_name)
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise RecoveryManifestError("manifest temporary file must be regular")
        identity = (created.st_dev, created.st_ino)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            descriptor = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        current = temporary.stat(follow_symlinks=False)
        if not stat.S_ISREG(current.st_mode):
            raise RecoveryManifestError("manifest temporary file must remain regular")
        if identity != (current.st_dev, current.st_ino):
            raise RecoveryManifestError("manifest temporary identity changed")
        os.link(temporary, target, follow_symlinks=False)
        published = True
        temporary.unlink()
        temporary = None
        _fsync_directory(root)
        return target
    except FileExistsError as exc:
        raise RecoveryManifestError("recovery manifest appeared before publication") from exc
    except OSError as exc:
        if published:
            _rollback_manifest(target)
        raise RecoveryManifestError("recovery manifest publication failed safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if published:
                    _rollback_manifest(target)
                raise RecoveryManifestError("manifest temporary cleanup failed safely") from exc


def _read_manifest(root: Path) -> bytes:
    path = _safe_entry(root, _MANIFEST_NAME, "recovery manifest")
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryManifestError("recovery manifest must be a regular file")
        if before.st_size > _MAX_MANIFEST_BYTES:
            raise RecoveryManifestError("recovery manifest exceeds safe size limit")
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            raw = handle.read(_MAX_MANIFEST_BYTES + 1)
            after = os.fstat(handle.fileno())
        if len(raw) > _MAX_MANIFEST_BYTES:
            raise RecoveryManifestError("recovery manifest exceeds safe size limit")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RecoveryManifestError("recovery manifest changed during verification")
        return raw
    except OSError as exc:
        raise RecoveryManifestError("recovery manifest cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _cleanup_current_attempt(root: Path, published_payloads: list[Path]) -> None:
    cleanup_cause: OSError | None = None
    cleanup_message: str | None = None
    for payload in reversed(published_payloads):
        try:
            payload.unlink(missing_ok=True)
            _fsync_directory(payload.parent)
        except OSError as exc:
            if cleanup_cause is None:
                cleanup_cause = exc
                cleanup_message = "backup attempt payload cleanup failed; package may remain"
    payload_root = root / "payload"
    if payload_root.exists() and not payload_root.is_symlink():
        try:
            payload_root.rmdir()
            _fsync_directory(root)
        except OSError as exc:
            if cleanup_cause is None:
                cleanup_cause = exc
                cleanup_message = "backup attempt directory cleanup failed; package may remain"
    if cleanup_cause is not None:
        assert cleanup_message is not None
        raise RecoveryManifestError(cleanup_message) from cleanup_cause


def _rollback_manifest(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
        _fsync_directory(target.parent)
    except OSError as exc:
        raise RecoveryManifestError("published recovery manifest rollback failed") from exc
