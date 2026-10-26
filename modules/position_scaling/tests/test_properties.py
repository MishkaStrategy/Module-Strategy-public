from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from modular_strategy_position_scaling.canonical import canonical_json
from modular_strategy_position_scaling.config import PositionScalingConfig
from modular_strategy_position_scaling.decimal_utils import weighted_average
from modular_strategy_position_scaling.engine import ScalingContext, ScalingPlanner
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    QuantityKind,
    ScalingMode,
    TriggerKind,
)
from modular_strategy_position_scaling.models import PositionAdjustmentPlan, ScalingRule
from modular_strategy_position_scaling.module import PositionScalingPlannerModule


def decimal_strategy(min_value: int = 1, max_value: int = 100000) -> st.SearchStrategy[Decimal]:
    return st.integers(min_value=min_value, max_value=max_value).map(
        lambda value: Decimal(value) / Decimal("100")
    )


@given(
    current_price=decimal_strategy(),
    added_price=decimal_strategy(),
    current_quantity=decimal_strategy(),
    added_quantity=decimal_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_weighted_average_is_within_price_bounds(
    current_price: Decimal,
    added_price: Decimal,
    current_quantity: Decimal,
    added_quantity: Decimal,
) -> None:
    result = weighted_average(current_price, current_quantity, added_price, added_quantity)
    assert min(current_price, added_price) <= result <= max(current_price, added_price)


@given(
    current_price=decimal_strategy(),
    added_price=decimal_strategy(),
    current_quantity=decimal_strategy(),
    added_quantity=decimal_strategy(),
)
@settings(max_examples=100, deadline=None)
def test_weighted_average_is_order_symmetric(
    current_price: Decimal,
    added_price: Decimal,
    current_quantity: Decimal,
    added_quantity: Decimal,
) -> None:
    first = weighted_average(current_price, current_quantity, added_price, added_quantity)
    second = weighted_average(added_price, added_quantity, current_price, current_quantity)
    assert first == second


def _context(*, side: str, current_price: Decimal, stop: Decimal) -> ScalingContext:
    return ScalingContext(
        position_id="p",
        setup_id="s",
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=stop,
        stop_price=stop,
        current_price=current_price,
        unrealized_pnl=Decimal("0"),
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
        source_feature_ids=(),
        features={},
    )


@given(distance_bps=st.integers(min_value=100, max_value=400))
@settings(max_examples=50, deadline=None)
def test_long_short_averaging_symmetry(distance_bps: int) -> None:
    distance = Decimal(distance_bps) / Decimal("10000")
    rule = ScalingRule(
        stage=1,
        mode=ScalingMode.AVERAGING_DOWN,
        action=AdjustmentAction.ADD,
        trigger_kind=TriggerKind.ADVERSE_PERCENT,
        trigger_value=distance,
        quantity_kind=QuantityKind.INITIAL_FRACTION,
        quantity_value=Decimal("0.25"),
        expires_bars=0,
    )
    config = PositionScalingConfig(
        enabled=True,
        allow_averaging_down=True,
        rules=(rule,),
    )
    planner = ScalingPlanner(config)
    long_price = Decimal("100") * (Decimal("1") - distance)
    short_price = Decimal("100") * (Decimal("1") + distance)
    long_plan = planner.evaluate_rule(
        _context(side="long", current_price=long_price, stop=Decimal("90")), rule
    ).plan
    short_plan = planner.evaluate_rule(
        _context(side="short", current_price=short_price, stop=Decimal("110")), rule
    ).plan
    assert long_plan is not None and short_plan is not None
    assert long_plan.desired_quantity == short_plan.desired_quantity
    assert long_plan.direction is short_plan.direction is AdjustmentDirection.INCREASE
    assert long_plan.current_total_risk == short_plan.current_total_risk


@given(quantity=decimal_strategy(1, 10000))
@settings(max_examples=50, deadline=None)
def test_plan_roundtrip_property(quantity: Decimal) -> None:
    rule = ScalingRule(
        stage=1,
        mode=ScalingMode.REBALANCE,
        action=AdjustmentAction.REBALANCE,
        trigger_kind=TriggerKind.ALWAYS,
        trigger_value=Decimal("0"),
        quantity_kind=QuantityKind.TARGET_QUANTITY,
        quantity_value=quantity + Decimal("1"),
        expires_bars=0,
    )
    config = PositionScalingConfig(
        enabled=True,
        allow_rebalance=True,
        rules=(rule,),
    )
    result = ScalingPlanner(config).evaluate_rule(
        _context(side="long", current_price=Decimal("101"), stop=Decimal("90")),
        rule,
    )
    if result.plan is None:
        return
    mapping = result.plan.to_mapping()
    assert PositionAdjustmentPlan.from_mapping(mapping).to_mapping() == mapping
    json.dumps(mapping)


def test_checkpoint_is_json_serializable() -> None:
    module = PositionScalingPlannerModule()
    payload = module.snapshot_state()
    json.dumps(payload)
    assert canonical_json(payload)
