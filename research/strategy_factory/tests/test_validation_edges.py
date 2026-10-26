from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import CandidateEvaluation, CandidateStrategy, RobustnessReport
from strategy_factory.errors import CompatibilityError, ContractError
from strategy_factory.validation import (
    CandidateGate,
    CandidateGatePolicy,
    CandidateGateResult,
    HoldoutAdmissionController,
    HoldoutAdmissionDecision,
    HoldoutIsolationGuard,
    MultipleTestingGuard,
    MultipleTestingResult,
    _decimal,
    _nonnegative_integral,
    _required_metric,
    _sha,
)

NOW = datetime(2026, 1, 2, tzinfo=UTC)
SHA = "a" * 64


def _candidate(
    architecture, candidate_id: str = "candidate-edge", **eval_changes
) -> CandidateStrategy:
    metrics = {
        "profit_factor": "1.5",
        "walk_forward_efficiency": "0.8",
        "parameter_stability": "0.8",
        "regime_coverage": "0.8",
        "time_coverage": "0.9",
        "cost_stress_profit_factor": "1.2",
        "year_stability": "0.8",
        "max_drawdown": "0.1",
        "trade_count": 100,
    }
    values = dict(
        evaluation_id=f"eval-{candidate_id}",
        candidate_id=candidate_id,
        metrics=metrics,
        constraints={"clean": True},
        pareto_front=0,
        instability_flags=(),
        complexity=Decimal("2"),
        evidence_references=("evidence",),
    )
    values.update(eval_changes)
    evaluation = CandidateEvaluation(**values)
    parameters = {"id": candidate_id}
    return CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=("run",),
        evaluation=evaluation,
    )


def _policy(**changes) -> CandidateGatePolicy:
    values = dict(minimum_trade_count=10, maximum_drawdown=Decimal("0.2"))
    values.update(changes)
    return CandidateGatePolicy(**values)


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"minimum_trade_count": True}, "positive integer"),
        ({"minimum_trade_count": 0}, "positive integer"),
        ({"maximum_drawdown": 1}, "finite non-negative Decimal"),
        ({"minimum_profit_factor": Decimal("NaN")}, "finite non-negative Decimal"),
        ({"minimum_parameter_stability": Decimal("1.1")}, "must not exceed"),
        ({"maximum_drawdown": Decimal("1.1")}, "must not exceed"),
        ({"maximum_complexity": Decimal("-1")}, "maximum_complexity"),
        ({"maximum_complexity": 1}, "maximum_complexity"),
    ],
)
def test_candidate_gate_policy_rejects_invalid(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        _policy(**changes)


def test_candidate_gate_result_contract() -> None:
    valid = CandidateGateResult("candidate", True, {"x": True}, {}, (), SHA)
    assert valid.eligible
    with pytest.raises(ContractError, match="candidate_id"):
        replace(valid, candidate_id="")
    with pytest.raises(ContractError, match="eligible must be bool"):
        replace(valid, eligible=1)
    with pytest.raises(ContractError, match="disagrees"):
        replace(valid, eligible=False, reason_codes=("bad",))
    with pytest.raises(ContractError, match="cannot contain"):
        replace(valid, reason_codes=("bad",))
    with pytest.raises(ContractError, match="requires rejection"):
        CandidateGateResult("candidate", False, {"x": False}, {}, (), SHA)
    with pytest.raises(ContractError, match="SHA-256"):
        replace(valid, policy_fingerprint="bad")


def test_holdout_guard_requires_evaluation_and_checks_references(architecture) -> None:
    candidate = _candidate(architecture)
    with pytest.raises(CompatibilityError, match="no evaluation"):
        HoldoutIsolationGuard.assert_unexposed(replace(candidate, evaluation=None))
    contaminated = replace(candidate, lineage_references=("final_untouched/result",))
    with pytest.raises(CompatibilityError, match="holdout references"):
        HoldoutIsolationGuard.assert_unexposed(contaminated)
    assert candidate.evaluation is not None
    contaminated = replace(
        candidate,
        evaluation=replace(candidate.evaluation, evidence_references=("untouched_test/result",)),
    )
    with pytest.raises(CompatibilityError, match="holdout references"):
        HoldoutIsolationGuard.assert_unexposed(contaminated)


def test_candidate_gate_all_rejection_paths(architecture) -> None:
    candidate = _candidate(architecture)
    assert candidate.evaluation is not None
    metrics = {
        "profit_factor": "0.5",
        "walk_forward_efficiency": "0.1",
        "parameter_stability": "0.1",
        "regime_coverage": "0.1",
        "time_coverage": "0.1",
        "cost_stress_profit_factor": "0.5",
        "year_stability": "0.1",
        "max_drawdown": "-0.5",
        "trade_count": 1,
    }
    rejected = replace(
        candidate,
        evaluation=replace(
            candidate.evaluation,
            metrics=metrics,
            constraints={"clean": False},
            instability_flags=("unstable",),
            complexity=Decimal("10"),
        ),
    )
    result = CandidateGate(_policy(maximum_complexity=Decimal("3"))).evaluate(rejected)
    assert not result.eligible
    assert {
        "LOW_TRADE_COUNT",
        "LOW_PROFIT_FACTOR",
        "UNSTABLE_WALK_FORWARD",
        "PARAMETER_INSTABILITY",
        "INSUFFICIENT_REGIME_COVERAGE",
        "INSUFFICIENT_TIME_COVERAGE",
        "COST_SENSITIVITY",
        "UNSTABLE_YEARLY_RESULTS",
        "DRAWDOWN_LIMIT_EXCEEDED",
        "DECLARED_CONSTRAINT_FAILURE",
        "INSTABILITY_FLAGS_PRESENT",
        "COMPLEXITY_LIMIT_EXCEEDED",
    } <= set(result.reason_codes)
    assert result.evidence["instability_flags"] == "unstable"


def test_candidate_gate_suffix_metric_and_bad_trade_count(architecture) -> None:
    candidate = _candidate(architecture)
    assert candidate.evaluation is not None
    metrics = dict(candidate.evaluation.metrics)
    del metrics["profit_factor"]
    metrics["validation.profit_factor"] = "1.4"
    accepted = replace(candidate, evaluation=replace(candidate.evaluation, metrics=metrics))
    assert CandidateGate(_policy()).evaluate(accepted).eligible
    for value in ("1.5", "-1"):
        bad = dict(candidate.evaluation.metrics)
        bad["trade_count"] = value
        with pytest.raises(CompatibilityError, match="non-negative integer"):
            CandidateGate(_policy()).evaluate(
                replace(candidate, evaluation=replace(candidate.evaluation, metrics=bad))
            )


@pytest.mark.parametrize(
    ("value", "message"),
    [(True, "numeric"), (object(), "numeric"), ("NaN", "finite")],
)
def test_decimal_metric_rejects_invalid(value, message) -> None:
    with pytest.raises(CompatibilityError, match=message):
        _decimal(value, "metric")


def test_required_metric_missing_and_sha_validation() -> None:
    with pytest.raises(CompatibilityError, match="missing"):
        _required_metric({}, "profit_factor")
    with pytest.raises(ContractError, match="SHA-256"):
        _sha("A" * 64, "hash")
    assert _nonnegative_integral(Decimal("2"), "count") == 2


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"method": "other"}, "unsupported"),
        ({"adjusted_alpha": Decimal("0")}, r"in \(0, 1\)"),
        ({"accepted_candidate_ids": ("a",), "rejected_candidate_ids": ("a",)}, "overlapping"),
    ],
)
def test_multiple_testing_result_rejects_invalid(changes, message) -> None:
    values = dict(
        adjusted_alpha=Decimal("0.05"),
        adjusted_p_values={"a": Decimal("0.01")},
        accepted_candidate_ids=("a",),
        rejected_candidate_ids=(),
    )
    values.update(changes)
    with pytest.raises(ContractError, match=message):
        MultipleTestingResult(**values)


@pytest.mark.parametrize("alpha", [1, Decimal("NaN"), Decimal("0"), Decimal("1")])
def test_multiple_testing_guard_rejects_invalid_alpha(alpha) -> None:
    with pytest.raises(ContractError, match="family_alpha"):
        MultipleTestingGuard(family_alpha=alpha)


def test_multiple_testing_guard_rejects_inputs_and_stops_after_first_failure() -> None:
    guard = MultipleTestingGuard(family_alpha=Decimal("0.05"))
    with pytest.raises(CompatibilityError, match="requires p-values"):
        guard.evaluate({})
    with pytest.raises(CompatibilityError, match="ID"):
        guard.evaluate({"": Decimal("0.1")})
    with pytest.raises(CompatibilityError, match="finite Decimals"):
        guard.evaluate({"a": Decimal("2")})
    result = guard.evaluate({"a": Decimal("0.001"), "b": Decimal("0.5"), "c": Decimal("0.001")})
    assert "b" in result.rejected_candidate_ids


class _Authorizer:
    def __init__(self) -> None:
        self.calls = []

    def authorize_final_test(
        self, experiment_id, *, candidate_configuration_hashes, issued_at=None
    ):
        self.calls.append((experiment_id, candidate_configuration_hashes, issued_at))
        return {"grant": "ok"}


def _robust(candidate_id: str, passed: bool = True) -> RobustnessReport:
    return RobustnessReport(
        f"robust-{candidate_id}", candidate_id, passed, {}, () if passed else ("bad",), NOW
    )


def test_holdout_admission_decision_and_controller_validation(architecture) -> None:
    with pytest.raises(ContractError, match="experiment_id"):
        HoldoutAdmissionDecision("", ("c",), (SHA,), (SHA,), SHA, {})
    with pytest.raises(ContractError, match="identities"):
        HoldoutAdmissionDecision("exp", (), (), (), SHA, {})
    with pytest.raises(ContractError, match="configuration_hash"):
        HoldoutAdmissionDecision("exp", ("c",), ("bad",), (SHA,), SHA, {})
    with pytest.raises(TypeError, match="authorizer"):
        HoldoutAdmissionController(object(), maximum_finalists=1)
    with pytest.raises(ContractError, match="maximum_finalists"):
        HoldoutAdmissionController(_Authorizer(), maximum_finalists=True)

    candidate = _candidate(architecture)
    gate = CandidateGate(_policy()).evaluate(candidate)
    controller = HoldoutAdmissionController(_Authorizer(), maximum_finalists=2)
    with pytest.raises(CompatibilityError, match="count"):
        controller.authorize(experiment_id="exp", candidates=(), gates=(), robustness_reports=())
    with pytest.raises(CompatibilityError, match="duplicate"):
        controller.authorize(
            experiment_id="exp",
            candidates=(candidate, candidate),
            gates=(gate,),
            robustness_reports=(_robust(candidate.candidate_id),),
        )
    with pytest.raises(CompatibilityError, match="candidate gate"):
        controller.authorize(
            experiment_id="exp",
            candidates=(candidate,),
            gates=(),
            robustness_reports=(_robust(candidate.candidate_id),),
        )
    failed_gate = replace(gate, eligible=False, checks={"x": False}, reason_codes=("bad",))
    with pytest.raises(CompatibilityError, match="candidate gate"):
        controller.authorize(
            experiment_id="exp",
            candidates=(candidate,),
            gates=(failed_gate,),
            robustness_reports=(_robust(candidate.candidate_id),),
        )
    with pytest.raises(CompatibilityError, match="robustness"):
        controller.authorize(
            experiment_id="exp",
            candidates=(candidate,),
            gates=(gate,),
            robustness_reports=(),
        )
    with pytest.raises(CompatibilityError, match="producer configuration hashes"):
        controller.authorize(
            experiment_id="exp",
            candidates=(candidate,),
            gates=(gate,),
            robustness_reports=(_robust(candidate.candidate_id),),
            producer_configuration_hashes={},
        )


def test_holdout_admission_uses_native_hash_and_issued_at(architecture) -> None:
    candidate = _candidate(architecture)
    gate = CandidateGate(_policy()).evaluate(candidate)
    authorizer = _Authorizer()
    decision = HoldoutAdmissionController(authorizer, maximum_finalists=1).authorize(
        experiment_id="exp",
        candidates=(candidate,),
        gates=(gate,),
        robustness_reports=(_robust(candidate.candidate_id),),
        producer_configuration_hashes={candidate.candidate_id: "b" * 64},
        issued_at=NOW,
    )
    assert decision.native_configuration_hashes == ("b" * 64,)
    assert authorizer.calls == [("exp", ("b" * 64,), NOW)]
