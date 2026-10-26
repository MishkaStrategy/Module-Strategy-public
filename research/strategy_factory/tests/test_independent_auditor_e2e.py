from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from strategy_audit_validation import (
    AuditPolicy,
    EvidenceValidator,
    HmacDecisionSigner,
    IndependentStrategyAuditor,
    SQLiteAuditStore,
    StaticDigestDatasetVerifier,
    StaticDigestResultsVerifier,
)

from strategy_factory import IndependentStrategyAuditPort
from strategy_factory.audit_envelope import (
    AUDIT_CONTRACT_VERSION_V2,
    AuditResearchPeriods,
    DatasetAuditLineage,
    FactoryAuditEnvelopeBuilder,
    ResultsAuditEvidence,
)
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    FactoryStage,
    HoldoutReport,
    RobustnessReport,
    TimeRange,
)
from strategy_factory.errors import CompatibilityError
from strategy_factory.run import StrategyFactoryRun
from strategy_factory.stage_handlers import FinalAuditHandler

AUDIT_NOW = datetime(2026, 1, 5, tzinfo=UTC)
MANIFEST_HASH = "1" * 64
RESULT_HASH = "2" * 64
SIGNING_KEY = b"factory-independent-auditor-test-key-32-bytes-minimum"


class DatasetSource:
    def __init__(self, lineage: DatasetAuditLineage) -> None:
        self._lineage = lineage

    def lineage_for(self, dataset):
        assert dataset.dataset_id == self._lineage.dataset_id
        return self._lineage


class ResultsSource:
    def __init__(self, evidence: ResultsAuditEvidence) -> None:
        self._evidence = evidence

    def evidence_for(self, candidate, *, experiment_id: str):
        assert candidate.candidate_id == self._evidence.candidate_id
        assert experiment_id in self._evidence.experiment_ids
        return self._evidence


def _candidate(architecture) -> CandidateStrategy:
    target = next(
        module for module in architecture.runtime_modules if module.module_id == "fixed_r"
    )
    parameters = {f"{target.runtime_component_id}.reward_r": "4.5"}
    evaluation = CandidateEvaluation(
        evaluation_id="evaluation-independent-auditor",
        candidate_id="candidate-independent-auditor",
        metrics={"parameter_stability": Decimal("0.90")},
        constraints={"registry_evidence_clean": True},
        pareto_front=1,
        instability_flags=(),
        complexity=Decimal("2"),
        evidence_references=("results-evidence-independent-auditor",),
    )
    return CandidateStrategy(
        candidate_id="candidate-independent-auditor",
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("factory-run-independent-auditor",),
        evaluation=evaluation,
    )


def _periods(start: datetime) -> AuditResearchPeriods:
    return AuditResearchPeriods(
        train=TimeRange(start, start + timedelta(days=5)),
        validation=TimeRange(start + timedelta(days=5), start + timedelta(days=10)),
        test=TimeRange(start + timedelta(days=10), start + timedelta(days=15)),
        holdout=TimeRange(start + timedelta(days=15), start + timedelta(days=20)),
    )


def _selection(candidate, request, periods: AuditResearchPeriods):
    robustness = RobustnessReport(
        report_id="robustness-independent-auditor",
        candidate_id=candidate.candidate_id,
        passed=True,
        scenario_results={"checks": {"stable": True}},
        failure_reasons=(),
        generated_at=AUDIT_NOW - timedelta(hours=3),
    )
    holdout = HoldoutReport(
        report_id="holdout-independent-auditor",
        candidate_id=candidate.candidate_id,
        period=periods.holdout,
        dataset_fingerprint=request.dataset.fingerprint,
        metrics={
            "profit_factor": Decimal("1.40"),
            "max_drawdown": Decimal("0.10"),
            "trade_count": 120,
        },
        passed=True,
        opened_once=True,
        generated_at=AUDIT_NOW - timedelta(hours=2),
    )
    return {
        "schema_version": 1,
        "selected": {
            "candidate": candidate,
            "robustness_report": robustness,
            "holdout_report": holdout,
            "authorization": {"grant_id": "grant-independent-auditor"},
            "experiment_id": "experiment-independent-auditor",
            "activation_run_ids": ("research-run-independent-auditor",),
            "native_configuration_hash": candidate.configuration_hash,
            "split": {
                "train": {"start": periods.train.start, "end": periods.train.end},
                "validation": {
                    "start": periods.validation.start,
                    "end": periods.validation.end,
                },
                "test": {"start": periods.test.start, "end": periods.test.end},
                "holdout": {
                    "start": periods.holdout.start,
                    "end": periods.holdout.end,
                },
                "gap_seconds": 0,
            },
            "selection_reason": "BEST_HOLDOUT_EVIDENCE_RETAINED",
            "dimensions": {},
        },
        "performance_anchor_candidate_id": candidate.candidate_id,
        "alternatives": (),
        "policy": {
            "profit_factor_tolerance": "0.05",
            "drawdown_tolerance": "0.02",
            "trade_count_drop_fraction": "0.25",
        },
        "comparison_dimensions": ("holdout_profit_factor",),
    }


def _auditor(
    path: Path, *, contract_version: str = "strategy-audit-port-v1"
) -> tuple[IndependentStrategyAuditor, SQLiteAuditStore]:
    policy = AuditPolicy()
    store = SQLiteAuditStore(path)
    validator = EvidenceValidator(
        policy=policy,
        dataset_verifier=StaticDigestDatasetVerifier({MANIFEST_HASH}),
        results_verifier=StaticDigestResultsVerifier({RESULT_HASH}),
    )
    return (
        IndependentStrategyAuditor(
            auditor_id="independent-strategy-auditor-e2e",
            policy=policy,
            validator=validator,
            signer=HmacDecisionSigner(SIGNING_KEY),
            store=store,
            clock=lambda: AUDIT_NOW,
            contract_version=contract_version,
        ),
        store,
    )


def test_factory_envelope_is_approved_and_replayed_after_auditor_restart(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture)
    periods = _periods(factory_request.dataset.period.start)
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path / "factory-state",
        module_versions={"fvg": "1.0.0"},
        now=AUDIT_NOW - timedelta(hours=4),
    )
    selection_id = run.store_artifact(
        artifact_type="simplicity_selection",
        value=_selection(candidate, factory_request, periods),
    )
    context = SimpleNamespace(
        request=factory_request,
        manifest=run.manifest,
        checkpoint=SimpleNamespace(run_id=run.run_id),
        artifact_id=lambda key: (
            selection_id if key == FactoryStage.SIMPLICITY_SELECTION.value else None
        ),
        load_artifact=lambda key: (
            run.load_artifact(selection_id)
            if key == FactoryStage.SIMPLICITY_SELECTION.value
            else None
        ),
        store_artifact=run.store_artifact,
    )
    lineage = DatasetAuditLineage(
        dataset_id=factory_request.dataset.dataset_id,
        registry_version="market-data-registry-v1",
        instrument=factory_request.symbol,
        exchange=factory_request.exchange,
        market_type=factory_request.market_type.value,
        manifest_sha256=MANIFEST_HASH,
        dataset_sha256=factory_request.dataset.sha256,
        timeframes=factory_request.dataset.available_timeframes,
        immutable=True,
        tamper_verified=True,
        funding_lineage_sha256="3" * 64,
    )
    results = ResultsAuditEvidence(
        candidate_id=candidate.candidate_id,
        configuration_hash=candidate.configuration_hash,
        experiment_ids=("experiment-independent-auditor",),
        registry_package_hashes=(RESULT_HASH,),
    )
    builder = FactoryAuditEnvelopeBuilder(
        dataset_source=DatasetSource(lineage),
        results_source=ResultsSource(results),
    )
    database = tmp_path / "independent-auditor.sqlite3"

    auditor, store = _auditor(database)
    first = FinalAuditHandler(
        IndependentStrategyAuditPort(auditor=auditor, run=run),
        envelope_builder=builder,
    ).execute(context)
    first_decision = first.artifact["audit_decision"]
    assert first_decision.status is AdmissionStatus.APPROVED_FOR_PAPER
    assert first_decision.evidence_hash == first.artifact["evidence_hash"]
    store.verify_integrity()
    store.close()

    restarted, restarted_store = _auditor(database)
    replay = FinalAuditHandler(
        IndependentStrategyAuditPort(auditor=restarted, run=run),
        envelope_builder=builder,
    ).execute(context)
    replay_decision = replay.artifact["audit_decision"]

    assert replay.artifact["evidence_artifact_id"] == first.artifact["evidence_artifact_id"]
    assert replay_decision.audit_id == first_decision.audit_id
    assert replay_decision.decision_hash == first_decision.decision_hash
    assert replay_decision.signature == first_decision.signature
    restarted_store.verify_integrity()
    count = restarted_store._connection.execute("SELECT COUNT(*) FROM audit_decisions").fetchone()[
        0
    ]
    assert count == 1
    restarted_store.close()


def test_factory_v2_envelope_roundtrips_through_real_auditor_as_research_only(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture)
    start = factory_request.dataset.period.start
    periods = AuditResearchPeriods(
        train=TimeRange(start, start + timedelta(minutes=25)),
        validation=TimeRange(start + timedelta(minutes=25), start + timedelta(minutes=50)),
        test=TimeRange(start + timedelta(minutes=50), start + timedelta(minutes=75)),
        holdout=TimeRange(start + timedelta(minutes=75), start + timedelta(minutes=100)),
    )
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path / "factory-state-v2",
        module_versions={"fvg": "1.0.0"},
        now=AUDIT_NOW - timedelta(hours=4),
    )
    selection_id = run.store_artifact(
        artifact_type="simplicity_selection",
        value=_selection(candidate, factory_request, periods),
    )
    context = SimpleNamespace(
        request=factory_request,
        manifest=run.manifest,
        checkpoint=SimpleNamespace(run_id=run.run_id),
        artifact_id=lambda key: (
            selection_id if key == FactoryStage.SIMPLICITY_SELECTION.value else None
        ),
        load_artifact=lambda key: (
            run.load_artifact(selection_id)
            if key == FactoryStage.SIMPLICITY_SELECTION.value
            else None
        ),
        store_artifact=run.store_artifact,
    )
    lineage = DatasetAuditLineage(
        dataset_id=factory_request.dataset.dataset_id,
        registry_version="market-data-registry-v1",
        instrument=factory_request.symbol,
        exchange=factory_request.exchange,
        market_type=factory_request.market_type.value,
        manifest_sha256=MANIFEST_HASH,
        dataset_sha256=factory_request.dataset.sha256,
        timeframes=factory_request.dataset.available_timeframes,
        immutable=True,
        tamper_verified=True,
        funding_lineage_sha256="3" * 64,
    )
    results = ResultsAuditEvidence(
        candidate_id=candidate.candidate_id,
        configuration_hash=candidate.configuration_hash,
        experiment_ids=("experiment-independent-auditor",),
        registry_package_hashes=(RESULT_HASH,),
    )
    builder = FactoryAuditEnvelopeBuilder(
        dataset_source=DatasetSource(lineage),
        results_source=ResultsSource(results),
        contract_version=AUDIT_CONTRACT_VERSION_V2,
    )
    auditor, store = _auditor(
        tmp_path / "independent-auditor-v2.sqlite3",
        contract_version=AUDIT_CONTRACT_VERSION_V2,
    )

    handler = FinalAuditHandler(
        IndependentStrategyAuditPort(auditor=auditor, run=run),
        envelope_builder=builder,
    )
    persisted_artifacts: list[dict[str, Any]] = []
    store_artifact = context.store_artifact

    def track_store_artifact(**kwargs):
        persisted_artifacts.append(kwargs)
        return store_artifact(**kwargs)

    context.store_artifact = track_store_artifact
    original_selection_id = selection_id
    credential_datasets = (
        replace(
            factory_request.dataset,
            uri="https://user:credential@example.test/data.csv?version=2",
        ),
        replace(
            factory_request.dataset,
            funding_uri="https://user:credential@example.test/funding.csv?version=2",
            funding_sha256="f" * 64,
        ),
    )
    for credential_dataset in credential_datasets:
        credential_request = replace(factory_request, dataset=credential_dataset)
        selection_id = run.store_artifact(
            artifact_type="simplicity_selection",
            value=_selection(candidate, credential_request, periods),
        )
        context.request = credential_request
        with pytest.raises(CompatibilityError, match="dataset URI") as error:
            handler.execute(context)
        assert "user:credential@" not in str(error.value)
        assert persisted_artifacts == []

    selection_id = original_selection_id
    context.request = factory_request
    result = handler.execute(context)
    decision = result.artifact["audit_decision"]
    envelope = run.load_artifact(result.artifact["evidence_artifact_id"])

    assert envelope["schema_version"] == 2
    assert envelope["package_intent"]["request_context"]["budget"]["stage_experiment_limits"]
    assert decision.contract_version == AUDIT_CONTRACT_VERSION_V2
    assert decision.status is AdmissionStatus.RESEARCH_ONLY, decision.reason_codes
    assert "PACKAGE_INTENT_CONTENT_BOUND" in decision.reason_codes
    assert "RESULT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
    assert "HOLDOUT_METRIC_SOURCE_EVIDENCE_UNAVAILABLE" in decision.reason_codes
    assert store.verify_integrity() is None
    store.close()
