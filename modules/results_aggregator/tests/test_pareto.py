from __future__ import annotations

from decimal import Decimal

import pytest

from modular_results_registry.errors import ValidationError
from modular_results_registry.models import MissingMetricPolicy, ObjectiveDirection
from modular_results_registry.pareto import (
    ConstraintResult,
    ParetoCandidate,
    ParetoObjective,
    rank_pareto,
)


def _candidate(
    candidate_id: str,
    *,
    return_pct: float | None,
    max_drawdown_pct: float | None,
    blocking_failure: bool = False,
    warnings: int = 0,
) -> ParetoCandidate:
    return ParetoCandidate(
        candidate_id=candidate_id,
        metrics={"return_pct": return_pct, "max_drawdown_pct": max_drawdown_pct},
        constraints=(
            ConstraintResult(
                "minimum_trades",
                passed=not blocking_failure,
                blocking=True,
                actual_value=10,
                threshold=100,
                reason="sample",
            ),
        ),
        warnings_count=warnings,
        oos_strength=Decimal("0.8"),
        robustness_strength=Decimal("0.7"),
        parameter_sensitivity=Decimal("0.1"),
        complexity=Decimal("1"),
        trade_count=200,
        configuration_hash=(candidate_id[-1] * 64),
    )


def _objectives() -> tuple[ParetoObjective, ...]:
    return (
        ParetoObjective("return_pct", ObjectiveDirection.MAXIMIZE),
        ParetoObjective("max_drawdown_pct", ObjectiveDirection.MINIMIZE),
    )


def test_pareto_frontier_and_dominated_candidates() -> None:
    result = rank_pareto(
        (
            _candidate("candidate-a", return_pct=10, max_drawdown_pct=5),
            _candidate("candidate-b", return_pct=8, max_drawdown_pct=6),
            _candidate("candidate-c", return_pct=12, max_drawdown_pct=8),
        ),
        _objectives(),
    )
    assert result.frontier == ("candidate-a", "candidate-c")
    assert result.dominated_by["candidate-b"] == ("candidate-a",)


def test_blocking_constraint_and_missing_metric_exclude_candidates() -> None:
    result = rank_pareto(
        (
            _candidate("candidate-a", return_pct=10, max_drawdown_pct=5),
            _candidate("candidate-b", return_pct=100, max_drawdown_pct=1, blocking_failure=True),
            _candidate("candidate-c", return_pct=None, max_drawdown_pct=1),
        ),
        _objectives(),
        missing_policy=MissingMetricPolicy.EXCLUDE,
    )
    assert result.frontier == ("candidate-a",)
    assert result.excluded["candidate-b"] == ("minimum_trades",)
    assert result.excluded["candidate-c"] == ("MISSING_METRIC:return_pct",)


def test_missing_metric_error_policy_is_fail_closed() -> None:
    with pytest.raises(ValidationError, match="missing objectives"):
        rank_pareto(
            (_candidate("candidate-a", return_pct=None, max_drawdown_pct=5),),
            _objectives(),
            missing_policy=MissingMetricPolicy.ERROR,
        )


def test_input_order_does_not_change_result_identity() -> None:
    candidates = (
        _candidate("candidate-a", return_pct=10, max_drawdown_pct=5),
        _candidate("candidate-b", return_pct=8, max_drawdown_pct=6),
    )
    left = rank_pareto(candidates, _objectives())
    right = rank_pareto(tuple(reversed(candidates)), tuple(reversed(_objectives())))
    assert left.result_id == right.result_id
    assert left.frontier == right.frontier


def test_epsilon_prevents_noise_from_creating_dominance() -> None:
    objectives = (
        ParetoObjective("return_pct", ObjectiveDirection.MAXIMIZE, Decimal("0.1")),
        ParetoObjective("max_drawdown_pct", ObjectiveDirection.MINIMIZE, Decimal("0.1")),
    )
    result = rank_pareto(
        (
            _candidate("candidate-a", return_pct=10.0, max_drawdown_pct=5.0),
            _candidate("candidate-b", return_pct=10.05, max_drawdown_pct=5.05),
        ),
        objectives,
    )
    assert set(result.frontier) == {"candidate-a", "candidate-b"}
