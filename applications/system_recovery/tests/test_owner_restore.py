from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from modular_research_runner import (
    AtomicJsonArtifactStore,
    BacktestJob,
    ExperimentStatus,
    RunStatus,
    SqliteExperimentRepository,
    restore_canonical_value,
)
from modular_system_acceptance import (
    AcceptanceCheckpoint,
    AcceptancePhase,
    AcceptanceRunSnapshot,
    AcceptanceRunSpec,
    AcceptanceStatus,
    EvidenceReference,
    SQLiteAcceptanceStore,
)
from modular_system_acceptance.storage import StoredRun

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
_FULL_RC_SHA = "1" * 40
_SOURCE_COMMIT = "4" * 40
_RELEASE_ID = "owner-restore-rc"
_DIGEST_A = "a" * 64
_DIGEST_B = "b" * 64
_DIGEST_C = "c" * 64


def _roots(tmp_path: Path) -> tuple[Path, Path, Path]:
    source = tmp_path / "source"
    backup = tmp_path / "backup"
    clean = tmp_path / "clean"
    source.mkdir()
    backup.mkdir()
    clean.mkdir()
    return source, backup, clean


def _integrity(path: Path) -> None:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()


def _acceptance_state() -> StoredRun:
    spec = AcceptanceRunSpec(
        full_rc_sha=_FULL_RC_SHA,
        dataset_id="dataset-restore",
        dataset_digest=_DIGEST_A,
        strategy_id="strategy-restore",
        strategy_digest=_DIGEST_B,
        config_digest=_DIGEST_C,
    )
    snapshot = AcceptanceRunSnapshot(
        acceptance_run_id="acceptance-restore-1",
        spec=spec,
        status=AcceptanceStatus.RECOVERING,
        phase=AcceptancePhase.RECOVERY,
        created_at=_AT,
        started_at=_AT,
        updated_at=_AT,
        completed_at=None,
        active_test_time=12.0,
        wall_clock_elapsed=15.0,
        restart_count=2,
        checkpoint=AcceptanceCheckpoint(
            phase=AcceptancePhase.RECOVERY,
            updated_at=_AT,
            token="checkpoint-restore-1",
            digest=_DIGEST_A,
        ),
        resources=(),
        warnings=("RECOVERY_TEST",),
        error=None,
        next_action="RESUME",
        evidence=(
            EvidenceReference(
                kind="recovery",
                digest=_DIGEST_B,
                reference="artifact-restore-1",
            ),
        ),
    )
    return StoredRun(
        snapshot=snapshot,
        phase_attempt=3,
        phase_attempt_id=None,
        phase_execution="IDLE",
        lease_owner=None,
        lease_expires_at=None,
        stop_requested=False,
        cancel_requested=False,
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


def test_system_acceptance_sqlite_restores_into_clean_root_with_owner_identity(
    tmp_path: Path,
) -> None:
    source, backup, clean = _roots(tmp_path)
    database = source / "system-acceptance" / "state.sqlite3"
    stored = _acceptance_state()
    owner = SQLiteAcceptanceStore(database)
    with owner.transaction() as connection:
        owner.save_run(stored, connection=connection)
        owner.insert_mutation(
            request_id="mutation-restore-1",
            fingerprint="restore-fingerprint",
            created_at=_AT,
            connection=connection,
        )
        owner.finish_mutation(
            request_id="mutation-restore-1",
            result={"accepted": True},
            error_code=None,
            completed_at=_AT,
            connection=connection,
        )
        owner.append_audit(
            timestamp=_AT,
            mutation="START",
            request_id="mutation-restore-1",
            operator="restore-admin",
            run_id=stored.snapshot.acceptance_run_id,
            result="SUCCEEDED",
            previous_status=None,
            new_status=stored.snapshot.status.value,
            phase=stored.snapshot.phase.value,
            connection=connection,
        )
    owner.close()

    entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="system-acceptance.sqlite",
            owner="system-acceptance",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source="system-acceptance/state.sqlite3",
            restore_target="system-acceptance/state.sqlite3",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(entry,),
    )
    restored_path = _restore(
        backup=backup,
        clean=clean,
        manifest=manifest,
        logical_id=entry.logical_id,
    )

    reopened = SQLiteAcceptanceStore(restored_path)
    assert reopened.load_run(stored.snapshot.acceptance_run_id) == stored
    with reopened.transaction() as connection:
        mutation = reopened.mutation_row("mutation-restore-1", connection=connection)
    assert mutation is not None
    assert mutation[1] == "SUCCEEDED"
    reopened.close()
    _integrity(restored_path)


def test_research_repository_and_protected_artifact_restore_with_restart_identity(
    tmp_path: Path,
) -> None:
    source, backup, clean = _roots(tmp_path)
    repository_path = source / "research" / "state.sqlite3"
    repository = SqliteExperimentRepository(repository_path)
    repository.register_experiment(
        experiment_id="experiment-restore-1",
        specification_sha256=_DIGEST_A,
        manifest={"schema_version": 1, "purpose": "restore-proof"},
        created_at=_AT,
    )
    job = BacktestJob(
        run_id="run-restore-1",
        experiment_id="experiment-restore-1",
        stage_id="stage-1",
        split_id="split-1",
        dataset_id="dataset-restore",
        scenario_id="scenario-restore",
        seed=7,
        parameters={"window": 20},
        output_directory=Path("outputs/run-restore-1"),
    )
    assert repository.register_jobs([job], max_attempts=3, created_at=_AT) == 1
    assert repository.queue_planned("experiment-restore-1", at=_AT) == 1

    artifact_store = AtomicJsonArtifactStore(source / "research" / "artifacts")
    artifact_reference = artifact_store.write_json(
        "results/result.json",
        {"experiment_id": "experiment-restore-1", "score": 1.25},
    )

    repository_entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="research.repository.sqlite",
            owner="research-runner",
            recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
            snapshot_kind=SnapshotKind.SQLITE,
            source="research/state.sqlite3",
            restore_target="research/state.sqlite3",
        ),
    )
    artifact_entry = snapshot_entry(
        source_root=source,
        backup_root=backup,
        spec=SnapshotSpec(
            logical_id="research.result.json",
            owner="research-runner",
            recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
            snapshot_kind=SnapshotKind.REGULAR_FILE,
            source="research/artifacts/results/result.json",
            restore_target="research/artifacts/results/result.json",
        ),
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=(repository_entry, artifact_entry),
    )

    restored_repository_path = _restore(
        backup=backup,
        clean=clean,
        manifest=manifest,
        logical_id=repository_entry.logical_id,
    )
    _restore(
        backup=backup,
        clean=clean,
        manifest=manifest,
        logical_id=artifact_entry.logical_id,
    )

    reopened_repository = SqliteExperimentRepository(restored_repository_path)
    assert reopened_repository.experiment_status("experiment-restore-1") is ExperimentStatus.CREATED
    claimed = reopened_repository.claim_next(
        experiment_id="experiment-restore-1",
        worker_id="restore-worker",
        session_id="restore-session",
        lease_token="restore-lease-token",
        at=_AT,
        lease_seconds=30,
    )
    assert claimed is not None
    restored_job, restored_state, restored_lease = claimed
    assert restored_job == job
    assert restored_state.status is RunStatus.RUNNING
    assert restored_lease.run_id == job.run_id
    assert restored_lease.worker_id == "restore-worker"
    assert restored_lease.session_id == "restore-session"
    assert restored_lease.lease_token == "restore-lease-token"
    assert restored_lease.revision == restored_state.revision

    reopened_artifacts = AtomicJsonArtifactStore(clean / "research" / "artifacts")
    assert restore_canonical_value(reopened_artifacts.read_json(artifact_reference)) == {
        "experiment_id": "experiment-restore-1",
        "score": 1.25,
    }
    _integrity(restored_repository_path)
