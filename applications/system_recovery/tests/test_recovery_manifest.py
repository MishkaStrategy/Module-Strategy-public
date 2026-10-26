from __future__ import annotations

import hashlib

import pytest

from system_recovery import (
    RecoveryClass,
    RecoveryEntry,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    manifest_from_payload,
)


def _entry(logical_id: str = "paper.state") -> RecoveryEntry:
    return RecoveryEntry(
        logical_id=logical_id,
        owner="paper-execution",
        recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
        snapshot_kind=SnapshotKind.SQLITE,
        source="paper/state.sqlite3",
        restore_target="paper/state.sqlite3",
        sha256="a" * 64,
        byte_size=42,
    )


def test_manifest_is_canonical_and_round_trips() -> None:
    manifest = RecoveryManifest(
        source_commit="b" * 40,
        platform_release_id="paper-rc.1",
        entries=(_entry(),),
    )

    rebuilt = manifest_from_payload(manifest.to_payload())

    assert rebuilt == manifest
    assert rebuilt.canonical_bytes() == manifest.canonical_bytes()
    assert rebuilt.sha256() == hashlib.sha256(manifest.canonical_bytes()).hexdigest()


def test_protected_immutable_evidence_round_trips_as_embedded_snapshot() -> None:
    entry = RecoveryEntry(
        logical_id="research.evidence",
        owner="research-runner",
        recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
        snapshot_kind=SnapshotKind.REGULAR_FILE,
        source="research/evidence.json",
        restore_target="research/evidence.json",
        sha256="e" * 64,
        byte_size=128,
    )
    manifest = RecoveryManifest(
        source_commit="b" * 40,
        platform_release_id="research-rc.1",
        entries=(entry,),
    )

    rebuilt = manifest_from_payload(manifest.to_payload())

    assert rebuilt == manifest
    assert rebuilt.entries[0].recovery_class is RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE
    assert rebuilt.entries[0].sha256 == "e" * 64
    assert rebuilt.entries[0].byte_size == 128


def test_manifest_rejects_duplicate_targets_and_unsorted_entries() -> None:
    with pytest.raises(RecoveryManifestError, match="sorted"):
        RecoveryManifest(
            source_commit="b" * 40,
            platform_release_id="paper-rc.1",
            entries=(_entry("z"), _entry("a")),
        )

    duplicate = RecoveryEntry(
        logical_id="paper.other",
        owner="paper-execution",
        recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
        snapshot_kind=SnapshotKind.REGULAR_FILE,
        source="paper/other.json",
        restore_target="paper/state.sqlite3",
        sha256="c" * 64,
        byte_size=2,
    )
    with pytest.raises(RecoveryManifestError, match="restore_target"):
        RecoveryManifest(
            source_commit="b" * 40,
            platform_release_id="paper-rc.1",
            entries=(duplicate, _entry()),
        )


def test_secret_cache_and_immutable_sources_are_reference_only() -> None:
    for recovery_class in (
        RecoveryClass.SECRET_PRIVATE,
        RecoveryClass.EPHEMERAL_CACHE,
        RecoveryClass.RECONSTRUCTABLE_IMMUTABLE,
    ):
        entry = RecoveryEntry(
            logical_id=f"reference.{recovery_class.value}",
            owner="system-recovery",
            recovery_class=recovery_class,
            snapshot_kind=SnapshotKind.REFERENCE,
            source="references/item",
            restore_target=None,
        )
        assert entry.sha256 is None

        with pytest.raises(RecoveryManifestError, match="cannot be embedded"):
            RecoveryEntry(
                logical_id=f"copied.{recovery_class.value}",
                owner="system-recovery",
                recovery_class=recovery_class,
                snapshot_kind=SnapshotKind.REGULAR_FILE,
                source="references/item",
                restore_target="restored/item",
                sha256="d" * 64,
                byte_size=1,
            )


def test_mutable_state_cannot_be_reference_only() -> None:
    with pytest.raises(RecoveryManifestError, match="reference-only"):
        RecoveryEntry(
            logical_id="paper.state",
            owner="paper-execution",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REFERENCE,
            source="paper/state.sqlite3",
            restore_target=None,
        )


@pytest.mark.parametrize("path", ["/absolute", "../escape", "a/../b", r"windows\path"])
def test_manifest_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(RecoveryManifestError, match="relative path"):
        RecoveryEntry(
            logical_id="paper.state",
            owner="paper-execution",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source=path,
            restore_target="paper/state.json",
            sha256="a" * 64,
            byte_size=1,
        )
