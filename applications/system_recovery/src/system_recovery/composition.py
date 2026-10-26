from __future__ import annotations

from pathlib import Path

from .manifest import RecoveryEntry, RecoveryManifest, RecoveryManifestError, SnapshotKind
from .restore import (
    _matches_identity,
    _prepare_destination,
    _restore_snapshot_entry_with_publication,
    _rollback_published,
    _safe_root,
)
from .snapshot import verify_snapshot_file


def restore_manifest_entries(
    *,
    backup_root: Path,
    restore_root: Path,
    manifest: RecoveryManifest,
    expected_source_commit: str,
    expected_platform_release_id: str,
) -> tuple[Path, ...]:
    """Restore all embedded manifest entries after a fail-closed global preflight.

    REFERENCE entries intentionally remain external. Embedded entries are admitted only
    when every backup payload and destination identity is valid before the first factual
    file is published. A complete exact restore is idempotent; a partial restore root is
    rejected rather than silently completed.
    """

    _verify_manifest_identity(
        manifest,
        expected_source_commit=expected_source_commit,
        expected_platform_release_id=expected_platform_release_id,
    )
    safe_backup = _safe_root(backup_root, "backup root")
    safe_restore = _safe_root(restore_root, "restore root")
    restorable = tuple(
        entry for entry in manifest.entries if entry.snapshot_kind is not SnapshotKind.REFERENCE
    )
    if not restorable:
        return ()

    for entry in restorable:
        verify_snapshot_file(safe_backup, entry)

    targets = tuple(_preflight_target(safe_restore, entry) for entry in restorable)
    existing = tuple(target.exists() for target in targets)
    if all(existing):
        return targets
    if any(existing):
        raise RecoveryManifestError("restore root contains a partial recovery publication")

    published: list[Path] = []
    restored_paths: list[Path] = []
    try:
        for entry in restorable:
            restored, was_published = _restore_snapshot_entry_with_publication(
                backup_root=safe_backup,
                restore_root=safe_restore,
                manifest=manifest,
                logical_id=entry.logical_id,
                expected_source_commit=expected_source_commit,
                expected_platform_release_id=expected_platform_release_id,
            )
            restored_paths.append(restored)
            if was_published:
                published.append(restored)
    except Exception:
        _rollback_current_attempt(published)
        raise
    return tuple(restored_paths)


def _verify_manifest_identity(
    manifest: RecoveryManifest,
    *,
    expected_source_commit: str,
    expected_platform_release_id: str,
) -> None:
    if manifest.source_commit != expected_source_commit:
        raise RecoveryManifestError("recovery manifest source commit mismatch")
    if manifest.platform_release_id != expected_platform_release_id:
        raise RecoveryManifestError("recovery manifest platform release mismatch")


def _preflight_target(root: Path, entry: RecoveryEntry) -> Path:
    if entry.restore_target is None:
        raise RecoveryManifestError("snapshot entry has no restore target")
    target = _prepare_destination(root, entry.restore_target)
    if target.is_symlink():
        raise RecoveryManifestError("restore target must not be a symbolic link")
    if target.exists() and not _matches_identity(target, entry):
        raise RecoveryManifestError("restore target already exists with different identity")
    return target


def _rollback_current_attempt(published: list[Path]) -> None:
    rollback_error: RecoveryManifestError | None = None
    for target in reversed(published):
        try:
            _rollback_published(target)
        except RecoveryManifestError as exc:
            if rollback_error is None:
                rollback_error = exc
    if rollback_error is not None:
        raise RecoveryManifestError(
            "platform restore rollback failed; destination may remain"
        ) from rollback_error
