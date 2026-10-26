from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from market_data_application.contracts import JobOperation
from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import AuditContext, SQLiteControlPlaneStore
from market_data_application.errors import RepairConfirmationRejectedError
from market_data_application.job_store import SQLiteJobStore
from market_data_application.lease_store import SQLiteJobLeaseStore
from market_data_application.repair_confirmation_store import SQLiteRepairConfirmationStore
from market_data_application.security import ApiRole
from market_data_ingestion.enums import DataType

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    RecoveryManifestError,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_AT = datetime(2099, 1, 2, 3, 4, 5, tzinfo=UTC)
_SOURCE_COMMIT = "7" * 40
_RELEASE_ID = "market-data-owner-restore-rc"
_AUDIT_KEY = b"market-data-recovery-audit-key-at-least-32-bytes"
_ADMIN_TOKEN = "market-data-admin-token-000000000000000000000000"
_REPAIR_TOKEN = "market-data-repair-token-0000000000000000000000"


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    clean = tmp_path / "clean"
    source.mkdir()
    backup.mkdir()
    clean.mkdir()
    return source, backup, clean


def _manifest(*entries) -> RecoveryManifest:
    return RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=tuple(sorted(entries, key=lambda item: item.logical_id)),
    )


def _restore(
    *,
    backup: Path,
    clean: Path,
    manifest: RecoveryManifest,
    logical_id: str,
) -> Path:
    return restore_snapshot_entry(
        backup_root=backup,
        restore_root=clean,
        manifest=manifest,
        logical_id=logical_id,
        expected_source_commit=_SOURCE_COMMIT,
        expected_platform_release_id=_RELEASE_ID,
    )


def _integrity(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def _jobs_entry(source: Path, backup: Path):
    return snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="market-data.jobs.sqlite",
            owner="market-data-ingestion",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source="market-data/jobs.sqlite3",
            restore_target="market-data/jobs.sqlite3",
        ),
    )


def test_market_data_jobs_repair_and_lease_state_restore_without_duplicate_authority(
    tmp_path: Path,
) -> None:
    source, backup, clean = _roots(tmp_path)
    database = source / "market-data" / "jobs.sqlite3"
    jobs = SQLiteJobStore(database)
    leases = SQLiteJobLeaseStore(database)
    confirmations = SQLiteRepairConfirmationStore(
        database,
        token_factory=lambda _size: _REPAIR_TOKEN,
    )

    request = {"dataset_id": "dataset-restore", "version": 3}
    created = jobs.create_job(
        operation=JobOperation.VALIDATE,
        request=request,
        idempotency_key="restore-idempotency-key",
        correlation_id="restore-correlation",
        actor_id="restore-operator",
    )
    original_lease = leases.claim(
        created.job.job_id,
        owner_id="worker-before-loss",
        ttl_seconds=30,
        at=_AT,
    )
    assert original_lease is not None

    issued = confirmations.issue(
        dataset_id="dataset-restore",
        version=3,
        preview_id="repair-preview:" + "a" * 24,
        action="derived_local_rebuild",
        selected_data_type=DataType.KLINES,
        acknowledged_mutations=("derived_partition_write",),
        actor_id="restore-operator",
        ttl_seconds=600,
        now=_AT,
    )
    consumed = confirmations.consume(
        issued.record.confirmation_id,
        token=_REPAIR_TOKEN,
        actor_id="restore-operator",
        preview_id=issued.record.preview_id,
        now=_AT + timedelta(seconds=1),
    )
    assert consumed.status == "consumed"

    entry = _jobs_entry(source, backup)
    manifest = _manifest(entry)
    restored_path = _restore(
        backup=backup,
        clean=clean,
        manifest=manifest,
        logical_id=entry.logical_id,
    )

    restored_jobs = SQLiteJobStore(restored_path)
    restored_leases = SQLiteJobLeaseStore(restored_path)
    restored_confirmations = SQLiteRepairConfirmationStore(restored_path)

    assert restored_jobs.get_job(created.job.job_id) == created.job
    replay = restored_jobs.create_job(
        operation=JobOperation.VALIDATE,
        request=request,
        idempotency_key="restore-idempotency-key",
        correlation_id="different-correlation-is-not-a-new-job",
        actor_id="restore-operator",
    )
    assert replay.reused is True
    assert replay.job.job_id == created.job.job_id

    assert (
        restored_leases.claim(
            created.job.job_id,
            owner_id="worker-after-restore",
            ttl_seconds=30,
            at=_AT + timedelta(seconds=10),
        )
        is None
    )
    replacement = restored_leases.claim(
        created.job.job_id,
        owner_id="worker-after-restore",
        ttl_seconds=30,
        at=_AT + timedelta(seconds=31),
    )
    assert replacement is not None
    assert replacement.generation == original_lease.generation + 1
    assert replacement.lease_token != original_lease.lease_token

    restored_confirmation = restored_confirmations.get(issued.record.confirmation_id)
    assert restored_confirmation.status == "consumed"
    with pytest.raises(RepairConfirmationRejectedError) as rejected:
        restored_confirmations.consume(
            issued.record.confirmation_id,
            token=_REPAIR_TOKEN,
            actor_id="restore-operator",
            preview_id=issued.record.preview_id,
            now=_AT + timedelta(seconds=32),
        )
    assert rejected.value.details["reason"] == "already_consumed"
    _integrity(restored_path)


def test_market_data_control_plane_restores_token_generation_and_hmac_audit_chain(
    tmp_path: Path,
) -> None:
    source, backup, clean = _roots(tmp_path)
    database = source / "market-data" / "control-plane.sqlite3"
    store = SQLiteControlPlaneStore(database, audit_key=_AUDIT_KEY)
    record = ApiTokenRecord.from_plaintext(
        token_id="restore-admin",
        token=_ADMIN_TOKEN,
        role=ApiRole.ADMIN,
        not_before=_AT,
    )
    state = TokenSnapshotState(
        current=ApiTokenSnapshot(
            generation=7,
            created_at=_AT,
            records=(record,),
        )
    )
    store.bootstrap(
        state,
        audit=AuditContext(
            timestamp=_AT,
            correlation_id="restore-bootstrap",
            direct_peer="local-process",
            principal_id="restore-admin",
            token_id="restore-admin",
            action="security.bootstrap",
            outcome="accepted",
            details={"generation": 7},
        ),
    )
    store.append_audit(
        AuditContext(
            timestamp=_AT + timedelta(seconds=1),
            correlation_id="restore-audit",
            direct_peer="127.0.0.1",
            principal_id="restore-admin",
            token_id="restore-admin",
            action="security.inspect",
            outcome="accepted",
            details={"purpose": "clean-host-recovery"},
        )
    )
    before_chain = store.verify_audit_chain()
    assert before_chain.valid is True
    assert before_chain.record_count == 2

    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="market-data.control-plane.sqlite",
            owner="market-data-control-plane",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source="market-data/control-plane.sqlite3",
            restore_target="market-data/control-plane.sqlite3",
        ),
    )
    manifest = _manifest(entry)
    restored_path = _restore(
        backup=backup,
        clean=clean,
        manifest=manifest,
        logical_id=entry.logical_id,
    )

    restored = SQLiteControlPlaneStore(restored_path, audit_key=_AUDIT_KEY)
    restored_state = restored.load_state()
    restored_chain = restored.verify_audit_chain()

    assert restored_state == state
    assert restored_state.current.generation == 7
    assert restored_chain == before_chain
    assert tuple(item.sequence for item in restored.audit_records()) == (1, 2)
    _integrity(restored_path)


def test_market_data_corrupt_owner_snapshot_fails_closed_before_clean_host_publication(
    tmp_path: Path,
) -> None:
    source, backup, clean = _roots(tmp_path)
    database = source / "market-data" / "jobs.sqlite3"
    jobs = SQLiteJobStore(database)
    jobs.create_job(
        operation=JobOperation.VALIDATE,
        request={"dataset_id": "dataset-corrupt", "version": 1},
        idempotency_key="corrupt-snapshot-idempotency",
    )

    entry = _jobs_entry(source, backup)
    manifest = _manifest(entry)
    (backup / "payload" / entry.logical_id).write_bytes(b"not-a-valid-sqlite-snapshot")

    with pytest.raises(RecoveryManifestError, match="snapshot"):
        _restore(
            backup=backup,
            clean=clean,
            manifest=manifest,
            logical_id=entry.logical_id,
        )

    assert not (clean / "market-data" / "jobs.sqlite3").exists()
