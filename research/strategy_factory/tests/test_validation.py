from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import CandidateEvaluation, CandidateStrategy, RobustnessReport
from strategy_factory.errors import CompatibilityError
from strategy_factory.validation import (
    CandidateGate,
    CandidateGatePolicy,
    HoldoutAdmissionController,
    HoldoutIsolationGuard,
    MultipleTestingGuard,
)


def _candidate(architecture, candidate_id: str = "candidate-validation") -> CandidateStrategy:
    metrics = {
        "profit_factor": "1.42",
        "walk_forward_efficiency": "0.72",
        "parameter_stability": "0.81",
        "regime_coverage": "0.75",
        "time_coverage": "0.95",
        "cost_stress_profit_factor": "1.12",
        "year_stability": "0.67",
        "max_drawdown": "0.09",
        "trade_count": 140,
    }
    evaluation = CandidateEvaluation(
        evaluation_id=f"eval-{candidate_id}",
        candidate_id=candidate_id,
        metrics=metrics,
        constraints={"registry_evidence_clean": True},
        pareto_front=1,
        instability_flags=(),
        complexity=Decimal("3"),
        evidence_references=("evidence-validation",),
    )
    parameters = {"fixed_r.reward_r": 4, "candidate": candidate_id}
    return CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("run-validation",),
        evaluation=evaluation,
    )


def _policy() -> CandidateGatePolicy:
    return CandidateGatePolicy(
        minimum_trade_count=30,
        maximum_drawdown=Decimal("0.15"),
        maximum_complexity=Decimal("5"),
    )


def test_candidate_gate_accepts_stable_evidence(architecture) -> None:
    result = CandidateGate(_policy()).evaluate(_candidate(architecture))
    assert result.eligible
    assert result.reason_codes == ()
    assert all(result.checks.values())


def test_candidate_gate_rejects_overfit_and_cost_sensitive(architecture) -> None:
    candidate = _candidate(architecture)
    assert candidate.evaluation is not None
    metrics = dict(candidate.evaluation.metrics)
    metrics.update(
        {
            "trade_count": 12,
            "walk_forward_efficiency": "0.20",
            "cost_stress_profit_factor": "0.82",
            "year_stability": "0.20",
        }
    )
    evaluation = replace(candidate.evaluation, metrics=metrics)
    result = CandidateGate(_policy()).evaluate(replace(candidate, evaluation=evaluation))
    assert not result.eligible
    assert set(result.reason_codes) >= {
        "LOW_TRADE_COUNT",
        "UNSTABLE_WALK_FORWARD",
        "COST_SENSITIVITY",
        "UNSTABLE_YEARLY_RESULTS",
    }


def test_holdout_contamination_fails_closed(architecture) -> None:
    candidate = _candidate(architecture)
    assert candidate.evaluation is not None
    contaminated = replace(
        candidate,
        evaluation=replace(
            candidate.evaluation,
            metrics={**dict(candidate.evaluation.metrics), "holdout.profit_factor": "3"},
        ),
    )
    with pytest.raises(CompatibilityError, match="holdout references"):
        HoldoutIsolationGuard.assert_unexposed(contaminated)


def test_metric_suffix_must_be_unique(architecture) -> None:
    candidate = _candidate(architecture)
    assert candidate.evaluation is not None
    metrics = dict(candidate.evaluation.metrics)
    del metrics["profit_factor"]
    metrics["validation.base.profit_factor"] = "1.3"
    metrics["test.base.profit_factor"] = "1.2"
    ambiguous = replace(candidate, evaluation=replace(candidate.evaluation, metrics=metrics))
    with pytest.raises(CompatibilityError, match="ambiguous"):
        CandidateGate(_policy()).evaluate(ambiguous)


def test_holm_bonferroni_counts_all_viewed_candidates() -> None:
    result = MultipleTestingGuard(family_alpha=Decimal("0.05")).evaluate(
        {
            "candidate-a": Decimal("0.001"),
            "candidate-b": Decimal("0.030"),
            "candidate-c": Decimal("0.040"),
        }
    )
    assert result.accepted_candidate_ids == ("candidate-a",)
    assert result.rejected_candidate_ids == ("candidate-b", "candidate-c")
    assert result.adjusted_p_values["candidate-a"] == Decimal("0.003")


class _Authorizer:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def authorize_final_test(
        self,
        experiment_id: str,
        *,
        candidate_configuration_hashes: tuple[str, ...],
    ) -> dict[str, object]:
        self.calls.append((experiment_id, candidate_configuration_hashes))
        return {"grant_id": "final-grant-native", "candidates": candidate_configuration_hashes}


def test_holdout_admission_delegates_to_research_runner(architecture) -> None:
    first = _candidate(architecture, "candidate-a")
    second = _candidate(architecture, "candidate-b")
    gate = CandidateGate(_policy())
    authorizer = _Authorizer()
    robustness = tuple(
        RobustnessReport(
            report_id=f"robustness-{candidate.candidate_id}",
            candidate_id=candidate.candidate_id,
            passed=True,
            scenario_results={"cost_stress": "pass"},
            failure_reasons=(),
            generated_at=candidate.evaluation
            and __import__("datetime").datetime.now(__import__("datetime").UTC),
        )
        for candidate in (first, second)
    )
    decision = HoldoutAdmissionController(authorizer, maximum_finalists=2).authorize(
        experiment_id="experiment-final",
        candidates=(second, first),
        gates=(gate.evaluate(first), gate.evaluate(second)),
        robustness_reports=robustness,
    )
    assert decision.candidate_ids == ("candidate-a", "candidate-b")
    assert authorizer.calls == [("experiment-final", decision.configuration_hashes)]
    assert decision.native_authorization["grant_id"] == "final-grant-native"


def test_holdout_admission_rejects_failed_robustness(architecture) -> None:
    candidate = _candidate(architecture)
    failed = RobustnessReport(
        report_id="robustness-failed",
        candidate_id=candidate.candidate_id,
        passed=False,
        scenario_results={},
        failure_reasons=("COST_SENSITIVITY",),
        generated_at=__import__("datetime").datetime.now(__import__("datetime").UTC),
    )
    with pytest.raises(CompatibilityError, match="robustness"):
        HoldoutAdmissionController(_Authorizer(), maximum_finalists=1).authorize(
            experiment_id="experiment-final",
            candidates=(candidate,),
            gates=(CandidateGate(_policy()).evaluate(candidate),),
            robustness_reports=(failed,),
        )
