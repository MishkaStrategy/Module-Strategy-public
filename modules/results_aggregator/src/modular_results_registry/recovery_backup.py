from __future__ import annotations

import hashlib
import os
import sqlite3
from pathlib import Path
from tempfile import NamedTemporaryFile

from . import recovery
from .canonical import canonical_json_bytes, canonical_sha256
from .errors import RecoveryError
from .storage import SQLiteRegistry


def create_backup(
    registry: SQLiteRegistry,
    database_path: str | Path,
    manifest_path: str | Path,
) -> recovery.BackupResult:
    target = Path(database_path)
    manifest_target = Path(manifest_path)
    recovery._ensure_safe_parent(target)
    recovery._ensure_safe_parent(manifest_target)
    if recovery._lexists(target) or recovery._lexists(manifest_target):
        raise RecoveryError("backup destination already exists")
    temp_db: Path | None = None
    temp_manifest: Path | None = None
    published_database = False
    try:
        with NamedTemporaryFile(dir=target.parent, prefix=".backup-", delete=False) as handle:
            temp_db = Path(handle.name)
        destination = sqlite3.connect(temp_db)
        try:
            registry._connection.backup(destination)
        finally:
            destination.close()
        recovery._fsync_regular_file(temp_db)
        with SQLiteRegistry(temp_db) as snapshot:
            snapshot_integrity = snapshot.verify_integrity()
            if not recovery._strict_integrity_passes(snapshot_integrity):
                raise RecoveryError("backup snapshot integrity check failed")
            snapshot_revision = snapshot.revision()
            snapshot_entity_hash = snapshot.entity_state_hash()
            snapshot_verified_hash = snapshot.verified_state_hash()
        database_sha, database_size = recovery._sha256_file(temp_db)
        manifest = {
            "schema_id": "modular-results-registry.backup",
            "schema_version": "1",
            "database_filename": target.name,
            "database_sha256": database_sha,
            "database_size": database_size,
            "registry_revision": snapshot_revision,
            "entity_state_hash": snapshot_entity_hash,
            "verified_state_hash": snapshot_verified_hash,
        }
        manifest["manifest_hash"] = canonical_sha256(manifest)
        with NamedTemporaryFile(
            "wb", dir=manifest_target.parent, prefix=".manifest-", delete=False
        ) as handle:
            temp_manifest = Path(handle.name)
            handle.write(canonical_json_bytes(manifest))
            handle.flush()
            os.fsync(handle.fileno())
        recovery._publish_temp_noreplace(temp_db, target)
        temp_db = None
        published_database = True
        try:
            recovery._publish_temp_noreplace(temp_manifest, manifest_target)
            temp_manifest = None
        except BaseException:
            recovery._remove_published_file(target)
            published_database = False
            raise
        published_database = False
        return recovery.BackupResult(
            database_path=target,
            manifest_path=manifest_target,
            database_sha256=database_sha,
            database_size=database_size,
            registry_revision=snapshot_revision,
            entity_state_hash=snapshot_entity_hash,
        )
    finally:
        if published_database:
            recovery._remove_published_file(target)
        if temp_db is not None:
            temp_db.unlink(missing_ok=True)
        if temp_manifest is not None:
            temp_manifest.unlink(missing_ok=True)


def restore_backup(
    database_path: str | Path,
    manifest_path: str | Path,
    destination_path: str | Path,
) -> Path:
    source = Path(database_path)
    manifest = recovery._load_manifest(Path(manifest_path))
    destination = Path(destination_path)
    recovery._ensure_safe_parent(destination)
    if recovery._lexists(destination):
        raise RecoveryError("restore destination already exists")
    temp: Path | None = None
    descriptor, before = recovery._open_regular_nofollow(source)
    try:
        with NamedTemporaryFile(dir=destination.parent, prefix=".restore-", delete=False) as handle:
            temp = Path(handle.name)
            digest = hashlib.sha256()
            total = 0
            while chunk := os.read(descriptor, recovery._CHUNK_BYTES):
                handle.write(chunk)
                digest.update(chunk)
                total += len(chunk)
            if not recovery._stable_after_read(before, os.fstat(descriptor), total):
                raise RecoveryError("backup database changed during restore")
            if digest.hexdigest() != manifest.get("database_sha256") or total != manifest.get(
                "database_size"
            ):
                raise RecoveryError("backup database checksum mismatch")
            handle.flush()
            os.fsync(handle.fileno())
        with SQLiteRegistry(temp) as restored:
            integrity = restored.verify_integrity()
            if not recovery._strict_integrity_passes(integrity):
                raise RecoveryError("restored registry integrity check failed")
            if restored.revision() != manifest["registry_revision"]:
                raise RecoveryError("restored registry revision mismatch")
            if restored.entity_state_hash() != manifest["entity_state_hash"]:
                raise RecoveryError("restored registry state hash mismatch")
            if restored.verified_state_hash() != manifest["verified_state_hash"]:
                raise RecoveryError("restored registry verified state hash mismatch")
        recovery._fsync_regular_file(temp)
        recovery._publish_temp_noreplace(temp, destination)
        temp = None
        return destination
    finally:
        os.close(descriptor)
        if temp is not None:
            temp.unlink(missing_ok=True)
