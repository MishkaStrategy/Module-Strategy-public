from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from modular_results_registry.canonical import canonical_sha256
from modular_results_registry.models import (
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
)

UTC = timezone.utc
NOW = datetime(2026, 7, 23, 8, 0, tzinfo=UTC)


def make_package(
    *,
    package_id: str = "package-001",
    experiment_id: str = "experiment-001",
    configuration_value: int = 1,
    run_id: str = "run-001",
    artifact_id: str = "artifact-001",
    metric_value: float = 1.5,
) -> RegistrationPackage:
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
        dataset_id="dataset-001",
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
        strategy_id="strategy-001",
        schema_version="1",
        canonical_configuration={"threshold": configuration_value},
        stage="research",
        execution_assumptions={"mode": "conservative"},
        risk_settings={"risk_per_trade": "0.5%"},
        cost_settings={"maker": "0.02%"},
        funding_settings={"scenario": "base"},
        creation_source="TEST",
    )
    experiment = ExperimentRecord(
        experiment_id=experiment_id,
        research_id="research-001",
        title="Test experiment",
        description="registry test",
        strategy_id="strategy-001",
        strategy_version="1.0.0",
        commit_sha="5e03b933207a99940244634f49a756d27888a5c3",
        branch="research/results-aggregator",
        working_tree_status="CLEAN",
        diff_hash=None,
        patch_artifact_id=None,
        module_versions={"core": "0.4.0", "strategy": "1.0.0"},
        dependency_lock_hash="c" * 64,
        started_at=NOW,
        status=ExperimentStatus.COMPLETED,
        search_method="grid",
        split_policy="walk-forward",
        completed_at=NOW,
        tags=("test",),
    )
    run = RunRecord(
        run_id=run_id,
        experiment_id=experiment_id,
        stage_id="stage-test",
        split_id="split-test",
        scenario_id="scenario-base",
        configuration_id=configuration.configuration_id,
        dataset_ids=(dataset.dataset_id,),
        seed=7,
        status=RunStatus.COMPLETED,
        started_at=NOW,
        ended_at=NOW,
        duration_seconds=1.0,
        worker_id="worker-local",
        machine_id="machine-local",
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
        value=metric_value,
        unit="ratio",
        precision=6,
        definition_version="v1",
        producer="core",
        split="test",
        period="all",
        scenario="base",
    )
    evidence = AuditEvidenceRecord(
        evidence_id="evidence-001",
        experiment_id=experiment_id,
        run_ids=(run_id,),
        evidence_type="bootstrap",
        producer="strategy-audit",
        producer_version="1",
        status=EvidenceStatus.PASS,
        severity=Severity.INFO,
        artifact_ids=(artifact_id,),
        created_at=NOW,
        scope={"samples": 100},
        evidence_hash="2" * 64,
    )
    role = RoleDecision(
        experiment_id=experiment_id,
        configuration_id=configuration.configuration_id,
        role=CandidateRole.CANDIDATE,
        reason="test candidate",
    )
    return RegistrationPackage(
        package_id=package_id,
        producer_module_id="test-producer",
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


@pytest.fixture
def package() -> RegistrationPackage:
    return make_package()


@pytest.fixture
def temp_db(tmp_path: Path) -> Path:
    return tmp_path / "registry.sqlite"


@pytest.fixture
def registry_path(temp_db: Path) -> Path:
    """Compatibility alias used by schema contract tests."""
    return temp_db
