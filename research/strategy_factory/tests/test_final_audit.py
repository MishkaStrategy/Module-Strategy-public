from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from strategy_factory.adapters import UnavailableStrategyAuditPort
from strategy_factory.audit_envelope import (
    AUDIT_CONTRACT_VERSION_V2,
    DatasetAuditLineage,
    FactoryArtifactEvidenceResolver,
    FactoryAuditEnvelopeBuilder,
    ResultsAuditEvidence,
)
from strategy_factory.canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from strategy_factory.contracts import (
    AdmissionStatus,
    CandidateEvaluation,
    CandidateStrategy,
    FactoryStage,
    HoldoutReport,
    RobustnessReport,
    StrategyAuditDecision,
)
from strategy_factory.errors import CompatibilityError, ExternalComponentUnavailableError
from strategy_factory.run import StrategyFactoryRun
from strategy_factory.stage_handlers import FinalAuditHandler, StrategyPackageHandler


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


def _candidate(architecture, candidate_id: str):
    parameters = {"fixed_fractional.risk_fraction": "0.005", "fixed_r.reward_r": 3}
    evaluation = CandidateEvaluation(
        evaluation_id=f"evaluation-{candidate_id}",
        candidate_id=candidate_id,
        metrics={"parameter_stability": Decimal("0.8")},
        constraints={"registry_evidence_clean": True},
        pareto_front=1,
        instability_flags=(),
        complexity=Decimal("2"),
        evidence_references=(f"evidence-{candidate_id}",),
    )
    return CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=(f"run-{candidate_id}",),
        evaluation=evaluation,
    )


def _split(factory_request):
    start = factory_request.dataset.period.start
    return {
        "train": {"start": start, "end": start + timedelta(minutes=25)},
        "validation": {
            "start": start + timedelta(minutes=25),
            "end": start + timedelta(minutes=50),
        },
        "test": {
            "start": start + timedelta(minutes=50),
            "end": start + timedelta(minutes=75),
        },
        "holdout": {
            "start": start + timedelta(minutes=75),
            "end": start + timedelta(minutes=100),
        },
        "gap_seconds": 0,
    }


def _record(candidate, factory_request):
    split = _split(factory_request)
    robustness = RobustnessReport(
        report_id=f"robustness-{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        passed=True,
        scenario_results={"checks": {"stable": True}},
        failure_reasons=(),
        generated_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    holdout = HoldoutReport(
        report_id=f"holdout-{candidate.candidate_id}",
        candidate_id=candidate.candidate_id,
        period=type(factory_request.dataset.period)(
            split["holdout"]["start"], split["holdout"]["end"]
        ),
        dataset_fingerprint=factory_request.dataset.fingerprint,
        metrics={
            "profit_factor": Decimal("1.25"),
            "max_drawdown": Decimal("0.10"),
            "trade_count": 100,
        },
        passed=True,
        opened_once=True,
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
    )
    return {
        "candidate": candidate,
        "robustness_report": robustness,
        "holdout_report": holdout,
        "authorization": {"grant_id": f"grant-{candidate.candidate_id}"},
        "experiment_id": f"experiment-{candidate.candidate_id}",
        "activation_run_ids": (f"final-{candidate.candidate_id}",),
        "native_configuration_hash": candidate.configuration_hash,
        "split": split,
    }


def _builder(
    factory_request,
    candidate,
    experiment_id: str,
    *,
    contract_version: str = "strategy-audit-port-v1",
):
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
        experiment_ids=(experiment_id,),
        registry_package_hashes=("3" * 64,),
    )
    return FactoryAuditEnvelopeBuilder(
        dataset_source=DatasetSource(lineage),
        results_source=ResultsSource(results),
        contract_version=contract_version,
    )


class AuditPort:
    contract_version = "strategy-audit-port-v1"

    def __init__(
        self,
        *,
        candidate_id: str,
        run: StrategyFactoryRun,
        issued_at: datetime,
        evidence_hash_override: str | None = None,
        contract_version: str = "strategy-audit-port-v1",
        status: AdmissionStatus = AdmissionStatus.APPROVED_FOR_PAPER,
    ) -> None:
        self.candidate_id = candidate_id
        self.resolver = FactoryArtifactEvidenceResolver(run)
        self.issued_at = issued_at
        self.evidence_hash_override = evidence_hash_override
        self.contract_version = contract_version
        self.status = status
        self.references: list[str] = []

    def audit(self, evidence_package_reference: str):
        self.references.append(evidence_package_reference)
        envelope = self.resolver(evidence_package_reference)
        evidence_hash = self.evidence_hash_override or canonical_sha256(envelope)
        return StrategyAuditDecision.issue(
            audit_id="audit-independent-001",
            candidate_id=self.candidate_id,
            status=self.status,
            evidence_package_reference=evidence_package_reference,
            evidence_hash=evidence_hash,
            reason_codes=("ALL_INDEPENDENT_GATES_PASSED",),
            issued_at=self.issued_at,
            auditor_id="strategy-audit-test",
            contract_version=self.contract_version,
            signature="test-signature-v1",
        )


def _context(factory_request, selection, tmp_path: Path):
    artifact_id = f"simplicity_selection-{canonical_sha256(selection)}"
    serialized = strict_json_loads(canonical_json_bytes(selection))
    run = StrategyFactoryRun(
        request=factory_request,
        commit_sha="a" * 40,
        state_root=tmp_path,
        module_versions={"fvg": "1.0.0"},
        now=datetime(2026, 1, 2, tzinfo=UTC),
    )
    return (
        SimpleNamespace(
            request=factory_request,
            manifest=run.manifest,
            checkpoint=SimpleNamespace(run_id=run.run_id),
            artifact_id=lambda key: (
                artifact_id if key == FactoryStage.SIMPLICITY_SELECTION.value else None
            ),
            load_artifact=lambda key: (
                serialized if key == FactoryStage.SIMPLICITY_SELECTION.value else None
            ),
            store_artifact=run.store_artifact,
        ),
        run,
    )


def _selection(candidate, factory_request):
    return {
        "schema_version": 1,
        "selected": {
            **_record(candidate, factory_request),
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


def test_final_audit_records_strict_external_decision(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture, "candidate-audit")
    selection = _selection(candidate, factory_request)
    context, run = _context(factory_request, selection, tmp_path)
    experiment_id = selection["selected"]["experiment_id"]
    port = AuditPort(
        candidate_id=candidate.candidate_id,
        run=run,
        issued_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    outcome = FinalAuditHandler(
        port,
        envelope_builder=_builder(factory_request, candidate, experiment_id),
    ).execute(context)
    decision = outcome.artifact["audit_decision"]
    envelope = port.resolver(outcome.artifact["evidence_package_reference"])

    assert decision.status is AdmissionStatus.APPROVED_FOR_PAPER
    assert decision.candidate_id == candidate.candidate_id
    assert decision.evidence_hash == canonical_sha256(envelope)
    assert outcome.artifact["evidence_artifact_id"].startswith("strategy_audit_envelope-")
    assert port.references == [outcome.artifact["evidence_package_reference"]]


def test_v2_final_audit_and_package_share_pre_audit_intent(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture, "candidate-v2-intent")
    selection = _selection(candidate, factory_request)
    context, run = _context(factory_request, selection, tmp_path)
    experiment_id = selection["selected"]["experiment_id"]
    port = AuditPort(
        candidate_id=candidate.candidate_id,
        run=run,
        issued_at=datetime(2026, 1, 3, tzinfo=UTC),
        contract_version=AUDIT_CONTRACT_VERSION_V2,
        status=AdmissionStatus.RESEARCH_ONLY,
    )
    result = FinalAuditHandler(
        port,
        envelope_builder=_builder(
            factory_request,
            candidate,
            experiment_id,
            contract_version=AUDIT_CONTRACT_VERSION_V2,
        ),
    ).execute(context)
    envelope = FactoryArtifactEvidenceResolver(run)(result.artifact["evidence_package_reference"])
    intent = result.artifact["package_intent"]

    assert envelope["schema_version"] == 2
    assert canonical_sha256(envelope["package_intent"]) == canonical_sha256(intent)
    assert intent["request_fingerprint"] == factory_request.fingerprint
    assert intent["intent_hash"] == canonical_sha256(
        {
            "schema_version": intent["schema_version"],
            "request_fingerprint": intent["request_fingerprint"],
            "request_context": intent["request_context"],
            "package_projection": intent["package_projection"],
        }
    )

    final_audit_artifact = strict_json_loads(canonical_json_bytes(result.artifact))
    selection_artifact = strict_json_loads(canonical_json_bytes(selection))
    context.load_artifact = lambda key: (
        final_audit_artifact if key == FactoryStage.FINAL_AUDIT.value else selection_artifact
    )
    context.artifact_id = lambda key: (
        "final_audit-stage-artifact" if key == FactoryStage.FINAL_AUDIT.value else None
    )
    package_result = StrategyPackageHandler().execute(context)
    package = package_result.artifact["strategy_package"]
    assert package.admission_status is AdmissionStatus.RESEARCH_ONLY
    assert package.constraints["package_intent_hash"] == intent["intent_hash"]
    assert "final_audit-stage-artifact" not in intent["package_projection"]["evidence_links"]


def test_final_audit_rejects_mismatched_evidence_hash(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture, "candidate-audit-bad")
    selection = _selection(candidate, factory_request)
    context, run = _context(factory_request, selection, tmp_path)
    experiment_id = selection["selected"]["experiment_id"]
    port = AuditPort(
        candidate_id=candidate.candidate_id,
        run=run,
        evidence_hash_override="f" * 64,
        issued_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    with pytest.raises(CompatibilityError, match="evidence hash mismatch"):
        FinalAuditHandler(
            port,
            envelope_builder=_builder(factory_request, candidate, experiment_id),
        ).execute(context)


def test_unavailable_audit_port_fails_closed(
    tmp_path: Path,
    factory_request,
    architecture,
) -> None:
    candidate = _candidate(architecture, "candidate-no-audit")
    selection = _selection(candidate, factory_request)
    context, _ = _context(factory_request, selection, tmp_path)
    experiment_id = selection["selected"]["experiment_id"]
    with pytest.raises(ExternalComponentUnavailableError):
        FinalAuditHandler(
            UnavailableStrategyAuditPort(),
            envelope_builder=_builder(factory_request, candidate, experiment_id),
        ).execute(context)
