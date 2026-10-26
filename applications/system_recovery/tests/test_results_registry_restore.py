from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from modular_results_registry import (
    ArtifactRecord,
    AuditEvidenceRecord,
    CandidateRole,
    ConfigurationRecord,
    DatasetRecord,
    EvidenceStatus,
    ExperimentRecord,
    ExperimentStatus,
    MetricDefinitionRecord,
    MetricSnapshot,
    RegistrationPackage,
    RoleDecision,
    RunRecord,
    RunStatus,
    Severity,
    SQLiteRegistry,
    canonical_sha256,
)
from modular_results_registry.errors import RecoveryError as ResultsRecoveryError
from modular_results_registry.recovery import create_backup, restore_backup

from system_recovery import (
    RecoveryClass,
    RecoveryManifest,
    SnapshotKind,
    SnapshotSpec,
    restore_snapshot_entry,
    snapshot_entry,
)

_AT = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
_SOURCE_COMMIT = "7" * 40
_RELEASE_ID = "results-registry-owner-restore"


def _package() -> RegistrationPackage:
    package_id = "package-restore-001"
    artifact_id = "artifact-restore-001"
    experiment_id = "experiment-restore-001"
    run_id = "run-restore-001"
    artifact = ArtifactRecord(
        artifact_id=artifact_id,
        package_id=package_id,
        relative_path="journal.json",
        media_type="application/json",
        size_bytes=2,
        sha256=canonical_sha256({}),
        producer_module_id="modular-core",
        producer_module_version="0.4.0",
        schema_id="journal",
        schema_version="1",
    )
    dataset = DatasetRecord(
        dataset_id="dataset-restore-001",
        source="local",
        market="BINANCE",
        market_type="USDTM_FUTURES",
        instrument="BTCUSDT",
        timeframe="5m",
        min_timestamp=datetime(2020, 1, 1, tzinfo=UTC),
        max_timestamp=datetime(2026, 7, 1, tzinfo=UTC),
        row_count=100,
        sha256="a" * 64,
        preprocessing_version="1",
        aggregation_version="1",
        metadata_hash="b" * 64,
        gap_summary={"gaps": 0},
        duplicate_summary={"duplicates": 0},
        quality_status="PASS",
    )
    configuration = ConfigurationRecord.create(
        strategy_id="strategy-restore-001",
        schema_version="1",
        canonical_configuration={"threshold": 1},
        stage="research",
        execution_assumptions={"mode": "conservative"},
        risk_settings={"risk_per_trade": "0.5%"},
        cost_settings={"maker": "0.02%"},
        funding_settings={"scenario": "base"},
        creation_source="RECOVERY_TEST",
    )
    experiment = ExperimentRecord(
        experiment_id=experiment_id,
        research_id="research-restore-001",
        title="Recovery experiment",
        description="clean-host recovery proof",
        strategy_id="strategy-restore-001",
        strategy_version="1.0.0",
        commit_sha="5e03b933207a99940244634f49a756d27888a5c3",
        branch="recovery/results-registry-owner-716",
        working_tree_status="CLEAN",
        diff_hash=None,
        patch_artifact_id=None,
        module_versions={"core": "0.4.0", "strategy": "1.0.0"},
        dependency_lock_hash="c" * 64,
        started_at=_AT,
        status=ExperimentStatus.COMPLETED,
        search_method="grid",
        split_policy="walk-forward",
        completed_at=_AT,
        tags=("recovery",),
    )
    run = RunRecord(
        run_id=run_id,
        experiment_id=experiment_id,
        stage_id="stage-restore",
        split_id="split-restore",
        scenario_id="scenario-base",
        configuration_id=configuration.configuration_id,
        dataset_ids=(dataset.dataset_id,),
        seed=7,
        status=RunStatus.COMPLETED,
        started_at=_AT,
        ended_at=_AT,
        duration_seconds=1.0,
        worker_id="worker-restore",
        machine_id="machine-restore",
        backtest_engine_version="0.4.0",
        metric_schema_version="metrics-v1",
        result_hash="d" * 64,
        producer_data_hash="e" * 64,
        producer_config_hash="f" * 64,
        producer_module_hash="1" * 64,
        symbol="BTCUSDT",
        base_timeframe="5m",
        bars_processed=100,
        module_names=("strategy",),
        artifact_ids=(artifact_id,),
    )
    definition = MetricDefinitionRecord(
        metric_name="profit_factor",
        definition_version="v1",
        formula="gross_profit / gross_loss",
        unit="ratio",
        precision=6,
        aggregation_method="AUTHORITATIVE_CORE_SNAPSHOT",
        missing_value_policy="PRESERVE_NULL_OR_REJECT_NONFINITE",
        producer="core",
        owner_module="modular_core.audit",
    )
    metric = MetricSnapshot(
        run_id=run_id,
        metric_name="profit_factor",
        value=1.5,
        unit="ratio",
        precision=6,
        definition_version="v1",
        producer="core",
        split="test",
        period="all",
        scenario="base",
    )
    evidence = AuditEvidenceRecord(
        evidence_id="evidence-restore-001",
        experiment_id=experiment_id,
        run_ids=(run_id,),
        evidence_type="bootstrap",
        producer="strategy-audit",
        producer_version="1",
        status=EvidenceStatus.PASS,
        severity=Severity.INFO,
        artifact_ids=(artifact_id,),
        created_at=_AT,
        scope={"samples": 100},
        evidence_hash="2" * 64,
    )
    role = RoleDecision(
        experiment_id=experiment_id,
        configuration_id=configuration.configuration_id,
        role=CandidateRole.CANDIDATE,
        reason="recovery candidate",
    )
    return RegistrationPackage(
        package_id=package_id,
        producer_module_id="recovery-test",
        producer_module_version="1",
        artifacts=(artifact,),
        datasets=(dataset,),
        configurations=(configuration,),
        experiments=(experiment,),
        runs=(run,),
        metric_definitions=(definition,),
        metrics=(metric,),
        evidence=(evidence,),
        roles=(role,),
    )


def _platform_stage_owner_backup(
    *,
    source_root: Path,
    backup_root: Path,
    clean_root: Path,
) -> tuple[Path, Path]:
    entries = tuple(
        sorted(
            (
                snapshot_entry(
                    source_root=source_root,
                    backup_root=backup_root,
                    spec=SnapshotSpec(
                        logical_id="results-registry.owner-backup.sqlite",
                        owner="results-registry",
                        recovery_class=RecoveryClass.MUTABLE_RUNTIME_STATE,
                        snapshot_kind=SnapshotKind.SQLITE,
                        source="owner-backup/registry.sqlite3",
                        restore_target="owner-backup/registry.sqlite3",
                    ),
                ),
                snapshot_entry(
                    source_root=source_root,
                    backup_root=backup_root,
                    spec=SnapshotSpec(
                        logical_id="results-registry.owner-backup.manifest",
                        owner="results-registry",
                        recovery_class=RecoveryClass.PROTECTED_IMMUTABLE_EVIDENCE,
                        snapshot_kind=SnapshotKind.REGULAR_FILE,
                        source="owner-backup/registry.manifest.json",
                        restore_target="owner-backup/registry.manifest.json",
                    ),
                ),
            ),
            key=lambda entry: entry.logical_id,
        )
    )
    manifest = RecoveryManifest(
        source_commit=_SOURCE_COMMIT,
        platform_release_id=_RELEASE_ID,
        entries=entries,
    )
    restored = {
        entry.logical_id: restore_snapshot_entry(
            backup_root=backup_root,
            restore_root=clean_root,
            manifest=manifest,
            logical_id=entry.logical_id,
            expected_source_commit=_SOURCE_COMMIT,
            expected_platform_release_id=_RELEASE_ID,
        )
        for entry in entries
    }
    return (
        restored["results-registry.owner-backup.sqlite"],
        restored["results-registry.owner-backup.manifest"],
    )


def test_results_registry_clean_host_restore_preserves_identity_and_continuation(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    backup_root = tmp_path / "platform-backup"
    clean_root = tmp_path / "clean"
    source_root.mkdir()
    backup_root.mkdir()
    clean_root.mkdir()
    owner_backup_dir = source_root / "owner-backup"
    owner_backup_dir.mkdir()

    package = _package()
    source_db = source_root / "results" / "registry.sqlite3"
    source_db.parent.mkdir()
    with SQLiteRegistry(source_db) as registry:
        registered = registry.register_package(package)
        assert registered.state == "REGISTERED"
        annotation_id, annotation_state, annotation_revision = registry.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="recovery-auditor",
            reason="RECOVERY_PROOF",
            payload={"decision": "retain"},
            created_at_utc=_AT.isoformat().replace("+00:00", "Z"),
        )
        assert annotation_state == "REGISTERED"
        expected_revision = annotation_revision
        expected_entity_hash = registry.entity_state_hash()
        expected_verified_hash = registry.verified_state_hash()
        expected_audit_hash = registry.verify_integrity()["audit_chain_hash"]
        create_backup(
            registry,
            owner_backup_dir / "registry.sqlite3",
            owner_backup_dir / "registry.manifest.json",
        )

    staged_db, staged_manifest = _platform_stage_owner_backup(
        source_root=source_root,
        backup_root=backup_root,
        clean_root=clean_root,
    )
    restored_db = clean_root / "results" / "registry.sqlite3"
    restore_backup(staged_db, staged_manifest, restored_db)

    with SQLiteRegistry(restored_db) as restored:
        assert restored.revision() == expected_revision
        assert restored.entity_state_hash() == expected_entity_hash
        assert restored.verified_state_hash() == expected_verified_hash
        integrity = restored.verify_integrity()
        assert integrity["sqlite_integrity"] == "ok"
        assert integrity["foreign_key_violations"] == 0
        assert integrity["audit_chain_hash"] == expected_audit_hash
        assert restored.get_payload("run", package.runs[0].run_id) is not None
        assert restored.get_payload("evidence", package.evidence[0].evidence_id) is not None
        assert restored.get_payload("role", package.roles[0].decision_id) is not None
        annotations = restored.annotations(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
        )
        assert annotations[0]["annotation_id"] == annotation_id

        duplicate = restored.register_package(package)
        assert duplicate.state == "DUPLICATE"
        assert duplicate.registry_revision == expected_revision
        assert restored.revision() == expected_revision

        continuation_id, continuation_state, continuation_revision = restored.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="recovery-auditor",
            reason="POST_RESTORE_CONTINUATION",
            payload={"decision": "continue"},
            created_at_utc="2026-08-12T12:00:01Z",
        )
        assert continuation_state == "REGISTERED"
        assert continuation_revision == expected_revision + 1
        repeated_id, repeated_state, repeated_revision = restored.add_annotation(
            entity_type="experiment",
            entity_id=package.experiments[0].experiment_id,
            actor="recovery-auditor",
            reason="POST_RESTORE_CONTINUATION",
            payload={"decision": "continue"},
            created_at_utc="2026-08-12T12:00:02Z",
        )
        assert repeated_id == continuation_id
        assert repeated_state == "DUPLICATE"
        assert repeated_revision == continuation_revision
        assert restored.revision() == continuation_revision


def test_results_registry_corrupt_owner_manifest_fails_closed(tmp_path: Path) -> None:
    source_db = tmp_path / "source.sqlite3"
    owner_backup = tmp_path / "owner-backup.sqlite3"
    owner_manifest = tmp_path / "owner-backup.json"
    package = _package()
    with SQLiteRegistry(source_db) as registry:
        registry.register_package(package)
        create_backup(registry, owner_backup, owner_manifest)

    owner_manifest.write_text("{}", encoding="utf-8")
    destination = tmp_path / "clean" / "registry.sqlite3"
    with pytest.raises(ResultsRecoveryError):
        restore_backup(owner_backup, owner_manifest, destination)
    assert not destination.exists()
