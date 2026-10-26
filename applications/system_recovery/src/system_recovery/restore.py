from __future__ import annotations

import hashlib
import os
import stat
import tempfile
from contextlib import suppress
from pathlib import Path, PurePosixPath

from .manifest import (
    RecoveryClass,
    RecoveryEntry,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
)
from .snapshot import verify_snapshot_file

_RESTORABLE_CLASSES = frozenset(
    {
        RecoveryClass.REQUIRED_CONFIGURATION,
        RecoveryClass.MUTABLE_RUNTIME_STATE,
        RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
    }
)


def restore_snapshot_entry(
    *,
    backup_root: Path,
    restore_root: Path,
    manifest: RecoveryManifest,
    logical_id: str,
    expected_source_commit: str,
    expected_platform_release_id: str,
) -> Path:
    """Restore one identity-bound manifest entry into a caller-provided clean root.

    Publication is fail-closed and no-overwrite. A repeated restore is accepted only
    when the already-published regular file exactly matches the manifest identity.
    """

    restored, _ = _restore_snapshot_entry_with_publication(
        backup_root=backup_root,
        restore_root=restore_root,
        manifest=manifest,
        logical_id=logical_id,
        expected_source_commit=expected_source_commit,
        expected_platform_release_id=expected_platform_release_id,
    )
    return restored


def _restore_snapshot_entry_with_publication(
    *,
    backup_root: Path,
    restore_root: Path,
    manifest: RecoveryManifest,
    logical_id: str,
    expected_source_commit: str,
    expected_platform_release_id: str,
) -> tuple[Path, bool]:
    """Restore one entry and report whether this call published the returned path."""

    if manifest.source_commit != expected_source_commit:
        raise RecoveryManifestError("recovery manifest source commit mismatch")
    if manifest.platform_release_id != expected_platform_release_id:
        raise RecoveryManifestError("recovery manifest platform release mismatch")
    entry = _manifest_entry(manifest, logical_id)
    if entry.snapshot_kind is SnapshotKind.REFERENCE:
        raise RecoveryManifestError("reference entries do not contain restorable bytes")
    if entry.recovery_class not in _RESTORABLE_CLASSES:
        raise RecoveryManifestError("recovery class is not restorable from backup payload")
    if entry.restore_target is None:
        raise RecoveryManifestError("snapshot entry has no restore target")
    if entry.sha256 is None or entry.byte_size is None:
        raise RecoveryManifestError("snapshot entry identity is incomplete")

    safe_backup = _safe_root(backup_root, "backup root")
    safe_restore = _safe_root(restore_root, "restore root")
    verify_snapshot_file(safe_backup, entry)
    source = _safe_entry(safe_backup, f"payload/{entry.logical_id}", "snapshot")
    target = _prepare_destination(safe_restore, entry.restore_target)

    if target.is_symlink():
        raise RecoveryManifestError("restore target must not be a symbolic link")
    if target.exists():
        if _matches_identity(target, entry):
            return target, False
        raise RecoveryManifestError("restore target already exists with different identity")

    _copy_verified_no_overwrite(source=source, target=target, entry=entry)
    return target, True


def _manifest_entry(manifest: RecoveryManifest, logical_id: str) -> RecoveryEntry:
    for entry in manifest.entries:
        if entry.logical_id == logical_id:
            return entry
    raise RecoveryManifestError("recovery manifest does not contain requested logical_id")


def _copy_verified_no_overwrite(*, source: Path, target: Path, entry: RecoveryEntry) -> None:
    source_descriptor = -1
    temporary_descriptor = -1
    temporary: Path | None = None
    published = False
    try:
        source_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            source_flags |= os.O_NOFOLLOW
        source_descriptor = os.open(source, source_flags)
        source_before = os.fstat(source_descriptor)
        if not stat.S_ISREG(source_before.st_mode):
            raise RecoveryManifestError("snapshot payload must be a regular file")

        temporary_descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".restore",
            dir=target.parent,
        )
        temporary = Path(temporary_name)
        digest = hashlib.sha256()
        copied = 0
        with (
            os.fdopen(source_descriptor, "rb", closefd=True) as source_handle,
            os.fdopen(temporary_descriptor, "wb", closefd=True) as target_handle,
        ):
            source_descriptor = -1
            temporary_descriptor = -1
            while chunk := source_handle.read(1024 * 1024):
                target_handle.write(chunk)
                digest.update(chunk)
                copied += len(chunk)
            target_handle.flush()
            os.fsync(target_handle.fileno())
            source_after = os.fstat(source_handle.fileno())

        if (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_mtime_ns,
        ) != (
            source_after.st_dev,
            source_after.st_ino,
            source_after.st_size,
            source_after.st_mtime_ns,
        ):
            raise RecoveryManifestError("snapshot payload changed while restore was reading it")
        if copied != entry.byte_size or digest.hexdigest() != entry.sha256:
            raise RecoveryManifestError(
                "snapshot payload identity changed before restore publication"
            )

        temporary_stat = temporary.stat(follow_symlinks=False)
        if not stat.S_ISREG(temporary_stat.st_mode):
            raise RecoveryManifestError("restore temporary file must remain regular")
        os.link(temporary, target, follow_symlinks=False)
        published = True
        temporary.unlink()
        temporary = None
        _fsync_directory(target.parent)
    except FileExistsError as exc:
        raise RecoveryManifestError("restore target appeared before publication") from exc
    except OSError as exc:
        if published:
            _rollback_published(target)
        raise RecoveryManifestError("snapshot restore failed safely") from exc
    finally:
        if source_descriptor >= 0:
            os.close(source_descriptor)
        if temporary_descriptor >= 0:
            os.close(temporary_descriptor)
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError as exc:
                if published:
                    _rollback_published(target)
                raise RecoveryManifestError("restore temporary cleanup failed safely") from exc


def _rollback_published(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise RecoveryManifestError("published restore rollback failed; target may remain") from exc
    try:
        _fsync_directory(target.parent)
    except OSError as exc:
        raise RecoveryManifestError(
            "published restore rollback durability could not be verified"
        ) from exc


def _matches_identity(path: Path, entry: RecoveryEntry) -> bool:
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryManifestError("existing restore target must be a regular file")
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                size += len(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RecoveryManifestError("existing restore target changed during verification")
        return size == entry.byte_size and digest.hexdigest() == entry.sha256
    except OSError as exc:
        raise RecoveryManifestError("existing restore target cannot be verified safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _safe_root(value: Path, name: str) -> Path:
    requested = Path(os.path.abspath(value.expanduser()))
    current = Path(requested.anchor)
    for part in requested.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise RecoveryManifestError(f"{name} must not traverse symbolic links")
    try:
        root = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RecoveryManifestError(f"{name} does not exist") from exc
    if not root.is_dir():
        raise RecoveryManifestError(f"{name} must be a directory")
    return root


def _safe_entry(root: Path, relative: str, name: str) -> Path:
    rel = _relative(relative, name)
    candidate = root.joinpath(*rel.parts)
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise RecoveryManifestError(f"{name} must not traverse symbolic links")
    try:
        resolved = candidate.resolve(strict=True)
    except FileNotFoundError as exc:
        raise RecoveryManifestError(f"{name} does not exist") from exc
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise RecoveryManifestError(f"{name} escapes declared root") from exc
    result = resolved.stat(follow_symlinks=False)
    if not stat.S_ISREG(result.st_mode):
        raise RecoveryManifestError(f"{name} must be a regular file")
    return resolved


def _prepare_destination(root: Path, relative: str) -> Path:
    rel = _relative(relative, "restore target")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        with suppress(FileExistsError):
            current.mkdir(mode=0o700)
        if current.is_symlink() or not current.is_dir():
            raise RecoveryManifestError("restore target parent must be a real directory")
        try:
            current.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, ValueError) as exc:
            raise RecoveryManifestError("restore target escapes declared root") from exc
    target = root.joinpath(*rel.parts)
    try:
        target.parent.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise RecoveryManifestError("restore target escapes declared root") from exc
    return target


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    descriptor = os.open(path, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _relative(value: str, name: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value:
        raise RecoveryManifestError(f"{name} must be a POSIX relative path")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise RecoveryManifestError(f"{name} must be a safe POSIX relative path")
    if str(path) != value:
        raise RecoveryManifestError(f"{name} must be a canonical POSIX relative path")
    return path
