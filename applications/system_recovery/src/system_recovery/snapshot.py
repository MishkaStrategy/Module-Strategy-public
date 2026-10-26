from __future__ import annotations

import hashlib
import os
import sqlite3
import stat
import tempfile
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from .manifest import (
    RecoveryClass,
    RecoveryEntry,
    RecoveryManifestError,
    SnapshotKind,
    _identifier,
    _relative_path,
)


@dataclass(frozen=True, slots=True)
class SnapshotSpec:
    logical_id: str
    owner: str
    recovery_class: RecoveryClass
    snapshot_kind: SnapshotKind
    source: str
    restore_target: str | None

    def __post_init__(self) -> None:
        _identifier(self.logical_id, "logical_id")
        _identifier(self.owner, "owner")
        _relative_path(self.source, "source")
        if self.restore_target is not None:
            _relative_path(self.restore_target, "restore_target")
        if self.snapshot_kind is SnapshotKind.REFERENCE:
            if self.recovery_class is RecoveryClass.MUTABLE_RUNTIME_STATE:
                raise RecoveryManifestError("mutable runtime state cannot be reference-only")
            return
        if self.recovery_class in {
            RecoveryClass.RECONSTRUCTABLE_IMMUTABLE,
            RecoveryClass.EPHEMERAL_CACHE,
            RecoveryClass.SECRET_PRIVATE,
        }:
            raise RecoveryManifestError("this recovery class must not be copied into a backup")
        if self.restore_target is None:
            raise RecoveryManifestError("snapshot entries require restore_target")


def snapshot_entry(*, source_root: Path, backup_root: Path, spec: SnapshotSpec) -> RecoveryEntry:
    source_root = _safe_root(source_root, "source root")
    backup_root = _safe_root(backup_root, "backup root")
    source = _safe_entry(source_root, spec.source, "source")

    if spec.snapshot_kind is SnapshotKind.REFERENCE:
        return RecoveryEntry(
            logical_id=spec.logical_id,
            owner=spec.owner,
            recovery_class=spec.recovery_class,
            snapshot_kind=spec.snapshot_kind,
            source=spec.source,
            restore_target=spec.restore_target,
        )
    if spec.recovery_class in {
        RecoveryClass.RECONSTRUCTABLE_IMMUTABLE,
        RecoveryClass.EPHEMERAL_CACHE,
        RecoveryClass.SECRET_PRIVATE,
    }:
        raise RecoveryManifestError("this recovery class must not be copied into a backup")
    if spec.restore_target is None:
        raise RecoveryManifestError("snapshot entries require restore_target")
    target = _safe_destination(backup_root, f"payload/{spec.logical_id}")

    if spec.snapshot_kind is SnapshotKind.SQLITE:
        sha256, byte_size = _snapshot_sqlite(source, target)
    elif spec.snapshot_kind is SnapshotKind.REGULAR_FILE:
        sha256, byte_size = _snapshot_regular_file(source, target)
    else:
        raise RecoveryManifestError("unsupported snapshot kind")

    return RecoveryEntry(
        logical_id=spec.logical_id,
        owner=spec.owner,
        recovery_class=spec.recovery_class,
        snapshot_kind=spec.snapshot_kind,
        source=spec.source,
        restore_target=spec.restore_target,
        sha256=sha256,
        byte_size=byte_size,
    )


def verify_snapshot_file(backup_root: Path, entry: RecoveryEntry) -> None:
    if entry.snapshot_kind is SnapshotKind.REFERENCE:
        return
    backup_root = _safe_root(backup_root, "backup root")
    path = _safe_entry(backup_root, f"payload/{entry.logical_id}", "snapshot")
    sha256, byte_size = _content_identity(path)
    if byte_size != entry.byte_size:
        raise RecoveryManifestError("snapshot byte size differs from manifest")
    if sha256 != entry.sha256:
        raise RecoveryManifestError("snapshot checksum differs from manifest")


def _snapshot_regular_file(source: Path, target: Path) -> tuple[str, int]:
    temp = target.with_name(f".{target.name}.tmp")
    descriptor = -1
    published = False
    temp_identity: tuple[int, int] | None = None
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(source, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryManifestError("state source must be a regular file")
        with os.fdopen(descriptor, "rb", closefd=True) as source_handle:
            descriptor = -1
            with temp.open("xb") as target_handle:
                created = os.fstat(target_handle.fileno())
                if not stat.S_ISREG(created.st_mode):
                    raise RecoveryManifestError("snapshot temporary file must be regular")
                temp_identity = (created.st_dev, created.st_ino)
                while chunk := source_handle.read(1024 * 1024):
                    target_handle.write(chunk)
                target_handle.flush()
                os.fsync(target_handle.fileno())
        after = _regular_stat(source)
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RecoveryManifestError("source changed while snapshot was being copied")
        temp_stat = _regular_stat(temp)
        if temp_identity != (temp_stat.st_dev, temp_stat.st_ino):
            raise RecoveryManifestError("snapshot temporary identity changed")
        sha256, byte_size = _content_identity(temp)
        os.link(temp, target, follow_symlinks=False)
        published = True
        temp.unlink()
        _fsync_directory(target.parent)
        return sha256, byte_size
    except FileExistsError as exc:
        raise RecoveryManifestError("snapshot destination appeared before publication") from exc
    except OSError as exc:
        if published:
            _rollback_snapshot(target)
        raise RecoveryManifestError("regular-file snapshot failed safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temp.unlink(missing_ok=True)
        except OSError as exc:
            if published:
                _rollback_snapshot(target)
            raise RecoveryManifestError("snapshot temporary cleanup failed safely") from exc


def _snapshot_sqlite(source: Path, target: Path) -> tuple[str, int]:
    before = _regular_stat(source)
    descriptor = -1
    temp: Path | None = None
    temp_identity: tuple[int, int] | None = None
    published = False
    try:
        descriptor, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.",
            suffix=".tmp",
            dir=target.parent,
        )
        temp = Path(temp_name)
        created = os.fstat(descriptor)
        if not stat.S_ISREG(created.st_mode):
            raise RecoveryManifestError("SQLite snapshot temporary file must be regular")
        temp_identity = (created.st_dev, created.st_ino)
        os.close(descriptor)
        descriptor = -1

        source_uri = f"file:{quote(source.as_posix(), safe='/')}?mode=ro"
        target_uri = f"file:{quote(temp.as_posix(), safe='/')}?mode=rw"
        with (
            sqlite3.connect(source_uri, uri=True, timeout=5.0) as source_db,
            sqlite3.connect(target_uri, uri=True, timeout=5.0) as target_db,
        ):
            source_db.backup(target_db)
            result = target_db.execute("PRAGMA integrity_check").fetchone()
            if result != ("ok",):
                raise RecoveryManifestError("SQLite backup failed integrity_check")

        after = _regular_stat(source)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise RecoveryManifestError("SQLite source identity changed during snapshot")
        temp_stat = _regular_stat(temp)
        if temp_identity != (temp_stat.st_dev, temp_stat.st_ino):
            raise RecoveryManifestError("SQLite snapshot temporary identity changed")
        sha256, byte_size = _content_identity(temp)
        os.link(temp, target, follow_symlinks=False)
        published = True
        temp.unlink()
        temp = None
        _fsync_directory(target.parent)
        return sha256, byte_size
    except FileExistsError as exc:
        raise RecoveryManifestError("snapshot destination appeared before publication") from exc
    except (OSError, sqlite3.Error) as exc:
        if published:
            _rollback_snapshot(target)
        raise RecoveryManifestError("SQLite snapshot failed safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError as exc:
                if published:
                    _rollback_snapshot(target)
                raise RecoveryManifestError(
                    "SQLite snapshot temporary cleanup failed safely"
                ) from exc


def _content_identity(path: Path) -> tuple[str, int]:
    descriptor = -1
    try:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(path, flags)
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise RecoveryManifestError("snapshot content must be a regular file")
        digest = hashlib.sha256()
        byte_size = 0
        with os.fdopen(descriptor, "rb", closefd=True) as handle:
            descriptor = -1
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                byte_size += len(chunk)
            after = os.fstat(handle.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        ):
            raise RecoveryManifestError("snapshot content changed during verification")
        return digest.hexdigest(), byte_size
    except OSError as exc:
        raise RecoveryManifestError("snapshot content cannot be read safely") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _rollback_snapshot(target: Path) -> None:
    try:
        target.unlink(missing_ok=True)
    except OSError as exc:
        raise RecoveryManifestError(
            "published snapshot rollback failed; target may remain"
        ) from exc
    try:
        _fsync_directory(target.parent)
    except OSError as exc:
        raise RecoveryManifestError(
            "published snapshot rollback durability could not be verified"
        ) from exc


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
    _regular_stat(resolved)
    return resolved


def _safe_destination(root: Path, relative: str) -> Path:
    rel = _relative(relative, "destination")
    current = root
    for part in rel.parts[:-1]:
        current = current / part
        with suppress(FileExistsError):
            current.mkdir(mode=0o700)
        if current.is_symlink() or not current.is_dir():
            raise RecoveryManifestError("destination parent must be a real directory")
        try:
            current.resolve(strict=True).relative_to(root)
        except (FileNotFoundError, ValueError) as exc:
            raise RecoveryManifestError("destination escapes backup root") from exc
    candidate = root.joinpath(*rel.parts)
    try:
        candidate.parent.resolve(strict=True).relative_to(root)
    except (FileNotFoundError, ValueError) as exc:
        raise RecoveryManifestError("destination escapes backup root") from exc
    if candidate.exists() or candidate.is_symlink():
        raise RecoveryManifestError("snapshot destination already exists")
    return candidate


def _regular_stat(path: Path) -> os.stat_result:
    try:
        result = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise RecoveryManifestError("state file cannot be inspected safely") from exc
    if not stat.S_ISREG(result.st_mode):
        raise RecoveryManifestError("state source must be a regular file")
    return result


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
