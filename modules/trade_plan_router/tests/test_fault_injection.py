from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    PlanVerdict,
    PositionState,
    ReasonCode,
    ScalingAction,
    ScalingDecision,
    ScalingDirection,
    TradeSide,
)
from modular_strategy_trade_plan_router.errors import PlanInvariantError

from conftest import NOW, make_package, make_portfolio


def _position() -> PositionState:
    return PositionState(
        position_id="position-fault",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=Decimal("5"),
        average_entry=Decimal("100"),
        version="position-fault-v1",
        market="usdt_perpetual",
        strategy_id="strategy-a",
        setup_id="setup-a",
        opened_at=NOW - timedelta(hours=1),
    )


def _package():
    scaling = ScalingDecision(
        decision_id="scale-fault",
        position_id="position-fault",
        setup_id="setup-a",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        action=ScalingAction.REDUCE,
        direction=ScalingDirection.DECREASE,
        approved_quantity=Decimal("2"),
        desired_price=Decimal("101"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    return make_package(
        entries=(),
        protections=(),
        sizings=(),
        scalings=(scaling,),
        portfolio=make_portfolio(positions=(_position(),)),
    )


def test_risk_reduction_composer_invariant_failure_becomes_typed_rejection(monkeypatch):
    router = DecisionRouter()

    def fail(*_args, **_kwargs):
        raise PlanInvariantError("injected-risk-reduction-invariant")

    monkeypatch.setattr(router._composer, "compose_risk_reduction", fail)
    result = router.route(_package())

    assert result.verdict is PlanVerdict.REJECTED
    assert result.plan is None
    assert result.reason_codes == (ReasonCode.PLAN_INVARIANT_FAILED,)
    assert result.audit_trace[-1].stage == "plan_invariants"
    assert result.audit_trace[-1].detail["detail"] == "injected-risk-reduction-invariant"


def test_missing_portfolio_rejection_reason_is_not_silently_invented(monkeypatch):
    router = DecisionRouter()

    def malformed_evaluation(*_args, **_kwargs):
        return None, None, "injected-missing-reason"

    monkeypatch.setattr(router._portfolio, "evaluate_risk_reduction", malformed_evaluation)
    with pytest.raises(PlanInvariantError, match="missing reason code"):
        router.route(_package())


def test_unexpected_risk_reduction_freshness_failure_propagates(monkeypatch):
    router = DecisionRouter()

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected-freshness-failure")

    monkeypatch.setattr(router._freshness, "linked_reason", boom)
    with pytest.raises(RuntimeError, match="injected-freshness-failure"):
        router.route(_package())


def test_unexpected_risk_reduction_constraint_failure_propagates(monkeypatch):
    router = DecisionRouter()

    def boom(*_args, **_kwargs):
        raise RuntimeError("injected-constraint-failure")

    monkeypatch.setattr(router._portfolio, "evaluate_risk_reduction", boom)
    with pytest.raises(RuntimeError, match="injected-constraint-failure"):
        router.route(_package())


def test_accepted_and_invariant_rejected_results_are_primitive_serializable(monkeypatch):
    accepted = DecisionRouter().route(_package())

    router = DecisionRouter()

    def fail(*_args, **_kwargs):
        raise PlanInvariantError("serialization-rejection")

    monkeypatch.setattr(router._composer, "compose_risk_reduction", fail)
    rejected = router.route(_package())

    for result in (accepted, rejected):
        mapping = result.to_mapping()
        assert mapping["verdict"] in {"accepted", "rejected"}
        assert all(isinstance(code, str) for code in mapping["reason_codes"])
        assert all(
            isinstance(code, str)
            for candidate in mapping["rejected_candidates"]
            for code in candidate["reason_codes"]
        )
