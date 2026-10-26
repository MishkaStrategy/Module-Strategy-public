from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from strategy_factory.audit_envelope import (
    AuditResearchPeriods,
    DatasetAuditLineage,
    FactoryArtifactEvidenceResolver,
    FactoryAuditEnvelopeBuilder,
    ResultsAuditEvidence,
)
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    CandidateEvaluation,
    CandidateStrategy,
    HoldoutReport,
    RobustnessReport,
    TimeRange,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.run import StrategyFactoryRun


class DatasetSource:
    def __init__(self, lineage: DatasetAuditLineage) -> None:
        self.lineage = lineage

    def lineage_for(self, dataset):
        del dataset
        return self.lineage


class ResultsSource:
    def __init__(self, evidence: ResultsAuditEvidence) -> None:
        self.evidence = evidence

    def evidence_for(self, candidate, *, experiment_id: str):
        del candidate, experiment_id
        return self.evidence


def _candidate(architecture) -> CandidateStrategy:
    parameters = {"protection_target.reward_r": 3, "variant": "audit-envelope"}
    evaluation = CandidateEvaluation(
        evaluation_id="evaluation-audit-envelope",
        candidate_id="candidate-audit-envelope",
        metrics={"parameter_stability": Decimal("0.8")},
        constraints={"registry_evidence_clean": True},
        pareto_front=1,
        instability_flags=(),
        complexity=Decimal("2"),
        evidence_references=("results-artifact-001",),
    )
    return CandidateStrategy(
        candidate_id="candidate-audit-envelope",
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("run-audit-envelope",),
        evaluation=evaluation,
    )


def _periods(start: datetime) -> AuditResearchPeriods:
    return AuditResearchPeriods(
        train=TimeRange(start, start + timedelta(minutes=25)),
        validation=TimeRange(start + timedelta(minutes=25), start + timedelta(minutes=50)),
        test=TimeRange(start + timedelta(minutes=50), start + timedelta(minutes=75)),
        holdout=TimeRange(start + timedelta(minutes=75), start + timedelta(minutes=100)),
    )


def _builder(factory_request, candidate: CandidateStrategy) -> FactoryAuditEnvelopeBuilder:
    lineage = DatasetAuditLineage(
        dataset_id=factory_request.dataset.dataset_id,
        registry_version="market-data-registry-v1",
        instrument=factory_request.symbol,
        exchange=factory_request.exchange,
        market_type=factory_request.market_type.value,
        manifest_sha256="1" * 64,
        dataset_sha256=factory_request.dataset.sha256,
        timeframes=factory_request.dataset.available_timeframes,
        immutable=True,
        tamper_verified=True,
        funding_lineage_sha256="2" * 64,
    )
    results = ResultsAuditEvidence(
        candidate_id=candidate.candidate_id,
        configuration_hash=candidate.configuration_hash,
        experiment_ids=("experiment-audit-envelope",),
        registry_package_hashes=("3" * 64,),
    )
    return FactoryAuditEnvelopeBuilder(
        dataset_source=DatasetSource(lineage),
        results_source=ResultsSource(results),
    )


def test_exact_envelope_binds_all_authoritative_evidence(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture)
    periods = _periods(factory_request.dataset.period.start)
    robustness = RobustnessReport(
        report_id="robustness-audit-envelope",
        candidate_id=candidate.candidate_id,
        passed=True,
        scenario_results={"checks": {"stable": True}},
        failure_reasons=(),
        generated_at=datetime(2026, 1, 2, 1, tzinfo=UTC),
    )
    holdout = HoldoutReport(
        report_id="holdout-audit-envelope",
        candidate_id=candidate.candidate_id,
        period=periods.holdout,
        dataset_fingerprint=factory_request.dataset.fingerprint,
        metrics={"profit_factor": "1.25", "max_drawdown": "0.10", "trade_count": 100},
        passed=True,
        opened_once=True,
        generated_at=datetime(2026, 1, 2, 2, tzinfo=UTC),
    )
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path,
        module_versions={"fvg": "1.0.0"},
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    envelope = _builder(factory_request, candidate).build(
        request=factory_request,
        manifest=run.manifest,
        candidate=candidate,
        robustness=robustness,
        holdout=holdout,
        periods=periods,
        experiment_id="experiment-audit-envelope",
    )

    assert set(envelope) == {
        "schema_version",
        "package_id",
        "created_at",
        "candidate",
        "dataset_lineage",
        "research",
        "robustness",
        "execution_assumptions",
        "risk_policy",
    }
    assert envelope["candidate"]["configuration_hash"] == candidate.configuration_hash
    assert envelope["dataset_lineage"]["manifest_sha256"] == "1" * 64
    assert envelope["research"]["results_evidence"]["registry_package_hashes"] == ("3" * 64,)
    assert envelope["execution_assumptions"] == {
        "mode": "paper",
        "exchange": factory_request.exchange,
        "market_type": factory_request.market_type.value,
        "live_trading_authorized": False,
        "private_exchange_access": False,
        "order_placement": False,
    }
    assert envelope["risk_policy"] == {
        "paper_only": True,
        "maximum_risk_fraction": format(factory_request.research_risk_fraction, "f"),
        "maximum_drawdown_fraction": format(
            factory_request.maximum_drawdown_fraction,
            "f",
        ),
    }


def test_envelope_rejects_cross_candidate_results(factory_request, architecture) -> None:
    candidate = _candidate(architecture)
    lineage = DatasetAuditLineage(
        dataset_id=factory_request.dataset.dataset_id,
        registry_version="market-data-registry-v1",
        instrument=factory_request.symbol,
        exchange=factory_request.exchange,
        market_type=factory_request.market_type.value,
        manifest_sha256="1" * 64,
        dataset_sha256=factory_request.dataset.sha256,
        timeframes=factory_request.dataset.available_timeframes,
        immutable=True,
        tamper_verified=True,
        funding_lineage_sha256="2" * 64,
    )
    builder = FactoryAuditEnvelopeBuilder(
        dataset_source=DatasetSource(lineage),
        results_source=ResultsSource(
            ResultsAuditEvidence(
                candidate_id="another-candidate",
                configuration_hash=candidate.configuration_hash,
                experiment_ids=("experiment-audit-envelope",),
                registry_package_hashes=("3" * 64,),
            )
        ),
    )
    periods = _periods(factory_request.dataset.period.start)
    robustness = RobustnessReport(
        report_id="robustness-audit-envelope",
        candidate_id=candidate.candidate_id,
        passed=True,
        scenario_results={},
        failure_reasons=(),
        generated_at=datetime(2026, 1, 2, 1, tzinfo=UTC),
    )
    holdout = HoldoutReport(
        report_id="holdout-audit-envelope",
        candidate_id=candidate.candidate_id,
        period=periods.holdout,
        dataset_fingerprint=factory_request.dataset.fingerprint,
        metrics={},
        passed=True,
        opened_once=True,
        generated_at=datetime(2026, 1, 2, 2, tzinfo=UTC),
    )
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=Path("unused"),
        module_versions={},
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )

    with pytest.raises(CompatibilityError, match="another candidate"):
        builder.build(
            request=factory_request,
            manifest=run.manifest,
            candidate=candidate,
            robustness=robustness,
            holdout=holdout,
            periods=periods,
            experiment_id="experiment-audit-envelope",
        )


def test_factory_artifact_resolver_rejects_non_envelope_reference(
    tmp_path: Path,
    factory_request,
) -> None:
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path,
        module_versions={},
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    resolver = FactoryArtifactEvidenceResolver(run)
    with pytest.raises(CompatibilityError, match="unsupported"):
        resolver("artifact:wrong")
