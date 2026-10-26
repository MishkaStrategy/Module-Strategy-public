from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from modular_strategy_position_scaling.config import PositionScalingConfig
from modular_strategy_position_scaling.engine import ScalingContext, ScalingPlanner
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    QuantityKind,
    ReasonCode,
    ScalingMode,
    TriggerKind,
)
from modular_strategy_position_scaling.models import ScalingRule


def _context(*, contract_size: object = "10") -> ScalingContext:
    return ScalingContext(
        position_id="position-1",
        setup_id="setup-1",
        symbol="BTCUSDT",
        side="long",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("95"),
        stop_price=Decimal("95"),
        current_price=Decimal("98"),
        unrealized_pnl=Decimal("-20"),
        equity=Decimal("10000"),
        cash=Decimal("9000"),
        index=10,
        attached_index=0,
        decision_time=datetime(2026, 1, 1, tzinfo=UTC),
        bar_seconds=60,
        last_adjustment_index=None,
        last_adjustment_price=None,
        scale_in_count=0,
        scale_out_count=0,
        data_gap=False,
        exit_pending=False,
        pending_order_count=0,
        setup_valid=True,
        market_regime="trend",
        atr=Decimal("2"),
        liquidation_distance_fraction=Decimal("0.2"),
        margin_usage_fraction=Decimal("0.1"),
        available_margin=Decimal("9000"),
        source_feature_ids=("instrument-1",),
        features={
            "instrument.rules": {
                "quantity_step": "0.01",
                "contract_size": contract_size,
                "max_leverage": "20",
            }
        },
    )


def _rule() -> ScalingRule:
    return ScalingRule(
        stage=1,
        mode=ScalingMode.AVERAGING_DOWN,
        action=AdjustmentAction.ADD,
        trigger_kind=TriggerKind.ALWAYS,
        trigger_value=Decimal("0"),
        quantity_kind=QuantityKind.INITIAL_FRACTION,
        quantity_value=Decimal("0.25"),
        expires_bars=1,
    )


def _planner(**overrides: object) -> ScalingPlanner:
    values: dict[str, object] = {
        "enabled": True,
        "rules": (_rule(),),
        "allow_averaging_down": True,
        "max_total_position_risk_fraction": Decimal("0.02"),
        "max_additional_risk_fraction": Decimal("0.01"),
        "max_total_leverage": Decimal("1"),
    }
    values.update(overrides)
    return ScalingPlanner(
        PositionScalingConfig(**values)  # type: ignore[arg-type]
    )


def test_contract_size_applies_to_plan_risk_and_notional() -> None:
    result = _planner().evaluate_rule(_context(), _rule())
    assert result.plan is not None
    assert result.plan.current_total_risk == Decimal("50")
    assert result.plan.projected_total_risk == Decimal("57.5")


def test_contract_size_can_block_planner_notional() -> None:
    result = _planner(max_total_notional=Decimal("1000")).evaluate_rule(
        _context(),
        _rule(),
    )
    assert result.plan is None
    assert result.reason is ReasonCode.MAX_TOTAL_NOTIONAL


def test_invalid_contract_size_fails_closed() -> None:
    result = _planner().evaluate_rule(
        _context(contract_size="0"),
        _rule(),
    )
    assert result.plan is None
    assert result.reason is ReasonCode.INVALID_RULE


def test_risk_request_contains_matching_typed_and_legacy_geometry() -> None:
    context = _context()
    planner = _planner()
    result = planner.evaluate_rule(context, _rule())
    assert result.plan is not None
    request = planner.build_risk_request(
        context,
        result.plan,
        {
            "quantity_step": "0.01",
            "contract_size": "10",
            "max_leverage": "20",
        },
    )
    typed = request["position_adjustment"]
    metadata = request["metadata"]
    assert isinstance(typed, dict)
    assert isinstance(metadata, dict)
    assert typed["schema_version"] == "1"
    assert typed["adjustment_id"] == metadata["adjustment_id"]
    assert typed["position_id"] == metadata["position_id"]
    assert typed["requested_quantity"] == metadata["requested_delta_quantity"]
    assert typed["projected_quantity"] == metadata["projected_total_quantity"]
    assert typed["valuation_price"] == metadata["valuation_price"] == "98"
    assert typed["current_notional"] == metadata["current_notional"] == "980"
    assert typed["projected_notional"] == metadata["projected_notional"] == "1225"


def test_risk_request_rejects_contract_size_mismatch() -> None:
    context = _context()
    planner = _planner()
    result = planner.evaluate_rule(context, _rule())
    assert result.plan is not None
    with pytest.raises(ValueError, match="contract_size disagrees"):
        planner.build_risk_request(
            context,
            result.plan,
            {"quantity_step": "0.01", "contract_size": "1"},
        )


def test_risk_request_rejects_tampered_plan_risk_geometry() -> None:
    context = _context()
    planner = _planner()
    result = planner.evaluate_rule(context, _rule())
    assert result.plan is not None
    tampered = replace(result.plan, current_total_risk=Decimal("49"))
    with pytest.raises(ValueError, match="risk geometry disagrees"):
        planner.build_risk_request(
            context,
            tampered,
            {"quantity_step": "0.01", "contract_size": "10"},
        )
