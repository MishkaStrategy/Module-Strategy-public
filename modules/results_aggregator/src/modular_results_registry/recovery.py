from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping

from .canonical import canonical_sha256
from .errors import IntegrityError, RecoveryError
from .safe_io import _open_directory_chain_nofollow

if TYPE_CHECKING:
    from .storage import SQLiteRegistry

_MAX_MANIFEST_BYTES = 4 * 1024 * 1024
_CHUNK_BYTES = 1024 * 1024
_SHA256_HEX = frozenset("0123456789abcdef")
_BACKUP_MANIFEST_FIELDS = frozenset(
    {
        "schema_id",
        "schema_version",
        "database_filename",
        "database_sha256",
        "database_size",
        "registry_revision",
        "entity_state_hash",
        "verified_state_hash",
        "manifest_hash",
    }
)

_ANNOTATION_DOCUMENT_FIELDS = frozenset(
    {
        "annotation_id",
        "entity_type",
        "entity_id",
        "actor",
        "reason",
        "previous_revision",
        "new_revision",
        "entity_hash",
        "payload_hash",
        "payload",
        "created_at_utc",
    }
)

_STRICT_INTEGRITY_FLAGS = (
    "audit_sequence_contiguous",
    "audit_revision_monotonic",
    "audit_revision_not_ahead",
    "audit_revision_coverage",
    "audit_event_ids_valid",
    "audit_bindings_valid",
    "audit_timestamps_valid",
    "audit_timestamp_monotonic",
    "operation_revision_coverage",
    "quarantine_records_valid",
    "entity_records_valid",
    "relation_records_valid",
    "annotation_records_valid",
    "annotation_actor_scope_constraint",
    "migration_records_valid",
    "append_only_triggers_present",
    "registered_revisions_valid",
)


def _lexists(path: Path) -> bool:
    return os.path.lexists(path)


def _ensure_safe_parent(path: Path) -> None:
    try:
        descriptor = _open_directory_chain_nofollow(path.absolute().parent, create=True)
    except IntegrityError as exc:
        raise RecoveryError("recovery path parent cannot be opened safely") from exc
    os.close(descriptor)


def _open_regular_nofollow(path: Path) -> tuple[int, os.stat_result]:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise RecoveryError(f"recovery source cannot be opened safely: {path}") from exc
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryError("recovery source must be a regular file")
        return descriptor, info
    except BaseException:
        os.close(descriptor)
        raise


def _stable_after_read(before: os.stat_result, after: os.stat_result, total: int) -> bool:
    return (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) == (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    ) and total == before.st_size


def _open_directory_nofollow(path: Path) -> int:
    try:
        return _open_directory_chain_nofollow(path)
    except IntegrityError as exc:
        raise RecoveryError("recovery destination parent cannot be opened safely") from exc


def _fsync_regular_file(path: Path) -> None:
    descriptor, _ = _open_regular_nofollow(path)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_temp_noreplace(temp: Path, target: Path) -> None:
    """Publish a complete same-directory temporary file without clobbering a race winner."""
    if temp.parent.absolute() != target.parent.absolute():
        raise RecoveryError("recovery temporary file must share the destination parent")
    parent_fd = _open_directory_nofollow(target.parent)
    linked = False
    try:
        try:
            temp_info = os.stat(temp.name, dir_fd=parent_fd, follow_symlinks=False)
        except OSError as exc:
            raise RecoveryError("recovery temporary file cannot be inspected") from exc
        if not stat.S_ISREG(temp_info.st_mode):
            raise RecoveryError("recovery temporary file must be regular")
        try:
            os.stat(target.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        except OSError as exc:
            raise RecoveryError("recovery destination cannot be inspected") from exc
        else:
            raise RecoveryError("recovery destination already exists")
        try:
            os.link(
                temp.name,
                target.name,
                src_dir_fd=parent_fd,
                dst_dir_fd=parent_fd,
                follow_symlinks=False,
            )
            linked = True
            os.fsync(parent_fd)
            os.unlink(temp.name, dir_fd=parent_fd)
            os.fsync(parent_fd)
            linked = False
        except FileExistsError as exc:
            raise RecoveryError("recovery destination already exists") from exc
        except OSError as exc:
            if linked:
                try:
                    os.unlink(target.name, dir_fd=parent_fd)
                    os.fsync(parent_fd)
                except OSError as rollback_exc:
                    raise RecoveryError(
                        "recovery publication failed and rollback was incomplete"
                    ) from rollback_exc
            raise RecoveryError("recovery destination publication failed") from exc
    finally:
        os.close(parent_fd)


def _remove_published_file(path: Path) -> None:
    parent_fd = _open_directory_nofollow(path.parent)
    try:
        try:
            info = os.stat(path.name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode):
            raise RecoveryError("published recovery target is no longer a regular file")
        os.unlink(path.name, dir_fd=parent_fd)
        os.fsync(parent_fd)
    except OSError as exc:
        raise RecoveryError("published recovery target rollback failed") from exc
    finally:
        os.close(parent_fd)


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256_HEX for character in value)
    )


def _validate_backup_manifest(value: Mapping[str, Any]) -> None:
    if set(value) != _BACKUP_MANIFEST_FIELDS:
        raise RecoveryError("backup manifest fields are invalid")
    if value.get("schema_id") != "modular-results-registry.backup":
        raise RecoveryError("unsupported backup manifest schema")
    if value.get("schema_version") != "1":
        raise RecoveryError("unsupported backup manifest schema")
    filename = value.get("database_filename")
    if (
        not isinstance(filename, str)
        or not filename
        or Path(filename).name != filename
        or filename in {".", ".."}
    ):
        raise RecoveryError("backup manifest database_filename is invalid")
    for field in (
        "database_sha256",
        "entity_state_hash",
        "verified_state_hash",
        "manifest_hash",
    ):
        if not _is_sha256(value.get(field)):
            raise RecoveryError(f"backup manifest {field} is invalid")
    for field in ("database_size", "registry_revision"):
        item = value.get(field)
        if isinstance(item, bool) or not isinstance(item, int) or item < 0:
            raise RecoveryError(f"backup manifest {field} is invalid")


def _strict_integrity_passes(report: Mapping[str, Any]) -> bool:
    return (
        report.get("sqlite_integrity") == "ok"
        and report.get("foreign_key_violations") == 0
        and all(report.get(field) is True for field in _STRICT_INTEGRITY_FLAGS)
    )


@dataclass(frozen=True, slots=True)
class BackupResult:
    database_path: Path
    manifest_path: Path
    database_sha256: str
    database_size: int
    registry_revision: int
    entity_state_hash: str


def _sha256_file(path: Path) -> tuple[str, int]:
    descriptor, before = _open_regular_nofollow(path)
    try:
        digest = hashlib.sha256()
        total = 0
        while chunk := os.read(descriptor, _CHUNK_BYTES):
            total += len(chunk)
            digest.update(chunk)
        if not _stable_after_read(before, os.fstat(descriptor), total):
            raise RecoveryError("recovery source changed during hashing")
        return digest.hexdigest(), total
    finally:
        os.close(descriptor)


def _load_manifest(path: Path) -> dict[str, Any]:
    descriptor, before = _open_regular_nofollow(path)
    try:
        if before.st_size > _MAX_MANIFEST_BYTES:
            raise RecoveryError("backup manifest exceeds size limit")
        chunks: list[bytes] = []
        total = 0
        while chunk := os.read(descriptor, min(_CHUNK_BYTES, _MAX_MANIFEST_BYTES + 1 - total)):
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_MANIFEST_BYTES:
                raise RecoveryError("backup manifest exceeds size limit")
        if not _stable_after_read(before, os.fstat(descriptor), total):
            raise RecoveryError("backup source changed during restore")

        def reject_constant(value: str) -> None:
            raise ValueError(f"non-finite JSON constant is forbidden: {value}")

        value = json.loads(
            b"".join(chunks).decode("utf-8"),
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryError("backup manifest is unreadable") from exc
    finally:
        os.close(descriptor)
    if not isinstance(value, dict):
        raise RecoveryError("backup manifest must be an object")
    expected = value.get("manifest_hash")
    actual = canonical_sha256({key: item for key, item in value.items() if key != "manifest_hash"})
    if expected != actual:
        raise RecoveryError("backup manifest hash mismatch")
    _validate_backup_manifest(value)
    return value


def create_backup(
    registry: "SQLiteRegistry",
    database_path: str | Path,
    manifest_path: str | Path,
) -> BackupResult:
    from .recovery_backup import create_backup as implementation

    return implementation(registry, database_path, manifest_path)


def restore_backup(
    database_path: str | Path,
    manifest_path: str | Path,
    destination_path: str | Path,
) -> Path:
    from .recovery_backup import restore_backup as implementation

    return implementation(database_path, manifest_path, destination_path)


def rebuild_from_bundle(bundle: Mapping[str, Any], destination_path: str | Path) -> Path:
    from .recovery_rebuild import rebuild_from_bundle as implementation

    return implementation(bundle, destination_path)


__all__ = [
    "BackupResult",
    "create_backup",
    "restore_backup",
    "rebuild_from_bundle",
]
