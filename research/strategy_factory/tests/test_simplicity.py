from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategy_factory.canonical import canonical_json_bytes, canonical_sha256, strict_json_loads
from strategy_factory.contracts import (
    CandidateEvaluation,
    CandidateStrategy,
    FactoryStage,
    HoldoutReport,
    RobustnessReport,
)
from strategy_factory.errors import ContractError
from strategy_factory.stage_handlers import (
    SimplicitySelectionHandler,
    SimplicitySelectionPolicy,
)


def _candidate(architecture, candidate_id: str, *, complexity: str, stability: str):
    parameters = {"protection_target.reward_r": 3, "variant": candidate_id}
    evaluation = CandidateEvaluation(
        evaluation_id=f"evaluation-{candidate_id}",
        candidate_id=candidate_id,
        metrics={
            "parameter_stability": Decimal(stability),
            "profit_factor": Decimal("1.2"),
        },
        constraints={"registry_evidence_clean": True},
        pareto_front=1,
        instability_flags=(),
        complexity=Decimal(complexity),
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


def _record(candidate, period, dataset_hash: str, *, profit_factor: str, drawdown: str):
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
        period=period,
        dataset_fingerprint=dataset_hash,
        metrics={
            "profit_factor": Decimal(profit_factor),
            "max_drawdown": Decimal(drawdown),
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
    }


def _context(factory_request, records):
    serialized = strict_json_loads(
        canonical_json_bytes({"schema_version": 1, "candidates": tuple(records)})
    )
    return SimpleNamespace(
        request=factory_request,
        checkpoint=SimpleNamespace(run_id="factory-run-simplicity"),
        load_artifact=lambda key: serialized if key == FactoryStage.HOLDOUT.value else None,
    )


def test_simplicity_prefers_less_complex_candidate_with_near_holdout_result(
    factory_request, architecture
) -> None:
    anchor = _candidate(architecture, "candidate-anchor", complexity="4", stability="0.75")
    simple = _candidate(architecture, "candidate-simple", complexity="2", stability="0.82")
    context = _context(
        factory_request,
        (
            _record(
                anchor,
                factory_request.dataset.period,
                factory_request.dataset.fingerprint,
                profit_factor="1.30",
                drawdown="0.10",
            ),
            _record(
                simple,
                factory_request.dataset.period,
                factory_request.dataset.fingerprint,
                profit_factor="1.27",
                drawdown="0.11",
            ),
        ),
    )
    outcome = SimplicitySelectionHandler().execute(context)
    selected = outcome.artifact["selected"]
    assert selected["candidate"].candidate_id == "candidate-simple"
    assert selected["selection_reason"] == "SIMPLER_CANDIDATE_WITHIN_HOLDOUT_TOLERANCE"
    assert outcome.finalists_selected == 1
    assert outcome.artifact["performance_anchor_candidate_id"] == "candidate-anchor"
    assert outcome.artifact["alternatives"][0]["candidate_id"] == "candidate-anchor"
    assert "HIGHER_COMPLEXITY" in outcome.artifact["alternatives"][0]["reason_codes"]


def test_simplicity_retains_anchor_when_alternative_is_outside_tolerance(
    factory_request, architecture
) -> None:
    anchor = _candidate(architecture, "candidate-anchor", complexity="5", stability="0.7")
    weak_simple = _candidate(architecture, "candidate-weak-simple", complexity="1", stability="0.9")
    context = _context(
        factory_request,
        (
            _record(
                anchor,
                factory_request.dataset.period,
                factory_request.dataset.fingerprint,
                profit_factor="1.30",
                drawdown="0.10",
            ),
            _record(
                weak_simple,
                factory_request.dataset.period,
                factory_request.dataset.fingerprint,
                profit_factor="1.10",
                drawdown="0.10",
            ),
        ),
    )
    outcome = SimplicitySelectionHandler().execute(context)
    assert outcome.artifact["selected"]["candidate"].candidate_id == "candidate-anchor"
    alternative = outcome.artifact["alternatives"][0]
    assert "OUTSIDE_PROFIT_FACTOR_TOLERANCE" in alternative["reason_codes"]


def test_simplicity_policy_rejects_invalid_tolerance() -> None:
    with pytest.raises(ContractError, match="profit_factor_tolerance"):
        SimplicitySelectionPolicy(profit_factor_tolerance=Decimal("-0.01"))
