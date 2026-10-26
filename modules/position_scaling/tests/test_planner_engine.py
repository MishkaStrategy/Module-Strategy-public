from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from modular_strategy_position_scaling.config import PositionScalingConfig
from modular_strategy_position_scaling.engine import ScalingContext, ScalingPlanner
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    QuantityKind,
    ReasonCode,
    ScalingMode,
    TriggerKind,
)
from modular_strategy_position_scaling.models import ScalingRule


def context(**overrides: object) -> ScalingContext:
    values: dict[str, object] = {
        "position_id": "position-1",
        "setup_id": "setup-1",
        "symbol": "BTCUSDT",
        "side": "long",
        "quantity": Decimal("1"),
        "average_price": Decimal("100"),
        "initial_quantity": Decimal("1"),
        "initial_entry_price": Decimal("100"),
        "initial_stop_price": Decimal("95"),
        "stop_price": Decimal("95"),
        "current_price": Decimal("98"),
        "unrealized_pnl": Decimal("-20"),
        "equity": Decimal("10000"),
        "cash": Decimal("9000"),
        "index": 10,
        "attached_index": 0,
        "decision_time": datetime(2026, 1, 1, tzinfo=UTC),
        "bar_seconds": 60,
        "last_adjustment_index": None,
        "last_adjustment_price": None,
        "scale_in_count": 0,
        "scale_out_count": 0,
        "data_gap": False,
        "exit_pending": False,
        "pending_order_count": 0,
        "setup_valid": True,
        "market_regime": "trend",
        "atr": Decimal("2"),
        "liquidation_distance_fraction": Decimal("0.2"),
        "margin_usage_fraction": Decimal("0.1"),
        "available_margin": Decimal("9000"),
        "source_feature_ids": ("f1",),
        "features": {"custom.flag": True},
    }
    values.update(overrides)
    return ScalingContext(**values)  # type: ignore[arg-type]


def rule(
    *,
    mode: ScalingMode = ScalingMode.AVERAGING_DOWN,
    action: AdjustmentAction = AdjustmentAction.ADD,
    trigger: TriggerKind = TriggerKind.ADVERSE_PERCENT,
    trigger_value: str = "0.01",
    quantity_kind: QuantityKind = QuantityKind.INITIAL_FRACTION,
    quantity_value: str = "0.25",
    explicit_price: str | None = None,
    feature_key: str | None = None,
) -> ScalingRule:
    return ScalingRule(
        stage=1,
        mode=mode,
        action=action,
        trigger_kind=trigger,
        trigger_value=Decimal(trigger_value),
        quantity_kind=quantity_kind,
        quantity_value=Decimal(quantity_value),
        explicit_price=None if explicit_price is None else Decimal(explicit_price),
        feature_key=feature_key,
        expires_bars=0,
    )


def config_for(mode: ScalingMode, **overrides: object) -> PositionScalingConfig:
    values: dict[str, object] = {
        "enabled": True,
        "rules": (rule(mode=mode),),
        "allow_averaging_down": mode is ScalingMode.AVERAGING_DOWN,
        "allow_pyramiding": mode is ScalingMode.PYRAMIDING,
        "allow_fixed_dca": mode is ScalingMode.FIXED_DCA,
        "allow_weighted_dca": mode is ScalingMode.WEIGHTED_DCA,
        "allow_scale_out": mode is ScalingMode.SCALE_OUT,
        "allow_rebalance": mode is ScalingMode.REBALANCE,
    }
    values.update(overrides)
    return PositionScalingConfig(**values)  # type: ignore[arg-type]


def planner_with(rule_value: ScalingRule, **config_overrides: object) -> ScalingPlanner:
    flags = {
        "allow_averaging_down": rule_value.mode is ScalingMode.AVERAGING_DOWN,
        "allow_pyramiding": rule_value.mode is ScalingMode.PYRAMIDING,
        "allow_fixed_dca": rule_value.mode is ScalingMode.FIXED_DCA,
        "allow_weighted_dca": rule_value.mode is ScalingMode.WEIGHTED_DCA,
        "allow_scale_out": rule_value.mode is ScalingMode.SCALE_OUT,
        "allow_rebalance": rule_value.mode is ScalingMode.REBALANCE,
    }
    flags.update(config_overrides)
    return ScalingPlanner(PositionScalingConfig(enabled=True, rules=(rule_value,), **flags))


def test_averaging_down_long_plan_math() -> None:
    planner = planner_with(rule())
    result = planner.evaluate_rule(context(), rule())
    assert result.plan is not None
    assert result.plan.direction is AdjustmentDirection.INCREASE
    assert result.plan.desired_quantity == Decimal("0.25")
    assert result.plan.expected_total_quantity == Decimal("1.25")
    assert result.plan.expected_average_price == Decimal("99.6")
    assert result.plan.risk_request_id is not None


def test_averaging_down_short_symmetry() -> None:
    planner = planner_with(rule())
    result = planner.evaluate_rule(
        context(
            side="short",
            current_price=Decimal("102"),
            initial_stop_price=Decimal("105"),
            stop_price=Decimal("105"),
        ),
        rule(),
    )
    assert result.plan is not None
    assert result.plan.expected_average_price == Decimal("100.4")
    assert result.plan.projected_total_risk == Decimal("5.75")


def test_pyramiding_requires_profit_r_trigger() -> None:
    pyramid_rule = rule(
        mode=ScalingMode.PYRAMIDING,
        trigger=TriggerKind.R_MULTIPLE,
        trigger_value="1",
    )
    planner = planner_with(pyramid_rule)
    assert (
        planner.evaluate_rule(context(current_price=Decimal("104")), pyramid_rule).reason
        is ReasonCode.NO_TRIGGER
    )
    result = planner.evaluate_rule(context(current_price=Decimal("105")), pyramid_rule)
    assert result.plan is not None


def test_pyramiding_with_protected_profit_stop_is_allowed() -> None:
    pyramid_rule = rule(
        mode=ScalingMode.PYRAMIDING,
        trigger=TriggerKind.R_MULTIPLE,
        trigger_value="1",
    )
    planner = planner_with(pyramid_rule)
    result = planner.evaluate_rule(
        context(current_price=Decimal("110"), stop_price=Decimal("102")),
        pyramid_rule,
    )
    assert result.plan is not None
    assert result.plan.current_total_risk == Decimal("0")


def test_add_rejects_stop_beyond_desired_entry() -> None:
    pyramid_rule = rule(
        mode=ScalingMode.PYRAMIDING,
        trigger=TriggerKind.ALWAYS,
        trigger_value="0",
    )
    planner = planner_with(pyramid_rule)
    result = planner.evaluate_rule(
        context(current_price=Decimal("101"), stop_price=Decimal("102")),
        pyramid_rule,
    )
    assert result.reason is ReasonCode.INVALID_GEOMETRY


@pytest.mark.parametrize(
    ("trigger_kind", "trigger_value", "current_price", "expected"),
    [
        (TriggerKind.ADVERSE_PERCENT, "0.02", "98", True),
        (TriggerKind.ADVERSE_PERCENT, "0.021", "98", False),
        (TriggerKind.ADVERSE_ABSOLUTE, "2", "98", True),
        (TriggerKind.FAVORABLE_PERCENT, "0.02", "102", True),
        (TriggerKind.FAVORABLE_ABSOLUTE, "2", "102", True),
        (TriggerKind.ATR_MULTIPLE, "1", "98", True),
        (TriggerKind.BARS, "10", "99", True),
    ],
)
def test_trigger_boundaries(
    trigger_kind: TriggerKind,
    trigger_value: str,
    current_price: str,
    expected: bool,
) -> None:
    chosen_mode = (
        ScalingMode.PYRAMIDING if "favorable" in trigger_kind.value else ScalingMode.AVERAGING_DOWN
    )
    chosen_rule = rule(mode=chosen_mode, trigger=trigger_kind, trigger_value=trigger_value)
    planner = planner_with(chosen_rule)
    result = planner.evaluate_rule(context(current_price=Decimal(current_price)), chosen_rule)
    assert (result.plan is not None) is expected


def test_price_ladder_long_and_short() -> None:
    ladder = rule(
        mode=ScalingMode.FIXED_DCA,
        trigger=TriggerKind.PRICE_LADDER,
        trigger_value="0",
        explicit_price="97",
    )
    planner = planner_with(ladder)
    assert planner.evaluate_rule(context(current_price=Decimal("97")), ladder).plan is not None
    short = context(
        side="short",
        current_price=Decimal("103"),
        stop_price=Decimal("105"),
        initial_stop_price=Decimal("105"),
    )
    assert planner.evaluate_rule(short, ladder).plan is not None


def test_feature_true_trigger() -> None:
    feature_rule = rule(
        trigger=TriggerKind.FEATURE_TRUE,
        trigger_value="0",
        feature_key="custom.flag",
    )
    planner = planner_with(feature_rule)
    assert planner.evaluate_rule(context(), feature_rule).plan is not None
    assert (
        planner.evaluate_rule(context(features={"custom.flag": False}), feature_rule).reason
        is ReasonCode.NO_TRIGGER
    )


def test_scale_out_fraction() -> None:
    scale_rule = rule(
        mode=ScalingMode.SCALE_OUT,
        action=AdjustmentAction.REDUCE,
        trigger=TriggerKind.R_MULTIPLE,
        trigger_value="1",
        quantity_kind=QuantityKind.CURRENT_FRACTION,
        quantity_value="0.25",
    )
    planner = planner_with(scale_rule)
    result = planner.evaluate_rule(context(current_price=Decimal("105")), scale_rule)
    assert result.plan is not None
    assert result.plan.direction is AdjustmentDirection.DECREASE
    assert result.plan.desired_quantity == Decimal("0.25")
    assert result.plan.expected_total_quantity == Decimal("0.75")
    assert result.plan.risk_request_id is None


@pytest.mark.parametrize(
    ("kind", "value", "direction", "quantity"),
    [
        (QuantityKind.TARGET_QUANTITY, "1.5", AdjustmentDirection.INCREASE, Decimal("0.5")),
        (QuantityKind.TARGET_QUANTITY, "0.5", AdjustmentDirection.DECREASE, Decimal("0.5")),
        (QuantityKind.TARGET_NOTIONAL, "150", AdjustmentDirection.INCREASE, Decimal("0.5")),
        (QuantityKind.EQUITY_FRACTION, "0.005", AdjustmentDirection.DECREASE, Decimal("0.5")),
    ],
)
def test_rebalance_targets(
    kind: QuantityKind,
    value: str,
    direction: AdjustmentDirection,
    quantity: Decimal,
) -> None:
    rebalance = rule(
        mode=ScalingMode.REBALANCE,
        action=AdjustmentAction.REBALANCE,
        trigger=TriggerKind.ALWAYS,
        trigger_value="0",
        quantity_kind=kind,
        quantity_value=value,
    )
    planner = planner_with(rebalance)
    result = planner.evaluate_rule(context(current_price=Decimal("100")), rebalance)
    assert result.plan is not None
    assert result.plan.direction is direction
    assert result.plan.desired_quantity == quantity


def test_stop_scaling_plan_has_no_quantity() -> None:
    stop_rule = rule(
        mode=ScalingMode.SCALE_OUT,
        action=AdjustmentAction.STOP_SCALING,
        trigger=TriggerKind.ALWAYS,
        trigger_value="0",
    )
    planner = planner_with(stop_rule)
    result = planner.evaluate_rule(context(), stop_rule)
    assert result.plan is not None
    assert result.plan.direction is AdjustmentDirection.NONE
    assert result.plan.desired_quantity == Decimal("0")


@pytest.mark.parametrize(
    ("context_override", "config_override", "expected"),
    [
        ({"data_gap": True}, {}, ReasonCode.DATA_GAP),
        ({"exit_pending": True}, {}, ReasonCode.EXIT_PENDING),
        ({"pending_order_count": 1}, {}, ReasonCode.CONFLICTING_PENDING_ORDER),
        ({"setup_valid": False}, {}, ReasonCode.SETUP_INVALID),
        ({"liquidation_distance_fraction": None}, {}, ReasonCode.LIQUIDATION_DISTANCE_UNKNOWN),
        (
            {"liquidation_distance_fraction": Decimal("0.01")},
            {},
            ReasonCode.LIQUIDATION_DISTANCE_TOO_SMALL,
        ),
        ({"margin_usage_fraction": None}, {}, ReasonCode.MARGIN_USAGE_UNKNOWN),
        ({"margin_usage_fraction": Decimal("0.9")}, {}, ReasonCode.MARGIN_USAGE_EXCEEDED),
        ({"scale_in_count": 2}, {}, ReasonCode.MAX_SCALE_IN_COUNT),
        ({"last_adjustment_index": 10}, {}, ReasonCode.MIN_BARS),
        ({"unrealized_pnl": Decimal("-300")}, {}, ReasonCode.MAX_POSITION_DRAWDOWN),
    ],
)
def test_safety_blocks(
    context_override: dict[str, object],
    config_override: dict[str, object],
    expected: ReasonCode,
) -> None:
    chosen_rule = rule(trigger=TriggerKind.ALWAYS, trigger_value="0")
    planner = planner_with(chosen_rule, **config_override)
    result = planner.evaluate_rule(context(**context_override), chosen_rule)
    assert result.reason is expected


def test_total_quantity_limit() -> None:
    chosen_rule = rule(trigger=TriggerKind.ALWAYS, trigger_value="0", quantity_value="0.5")
    planner = planner_with(chosen_rule, max_total_quantity=Decimal("1.4"))
    assert planner.evaluate_rule(context(), chosen_rule).reason is ReasonCode.MAX_TOTAL_QUANTITY


def test_total_notional_limit() -> None:
    chosen_rule = rule(trigger=TriggerKind.ALWAYS, trigger_value="0", quantity_value="0.5")
    planner = planner_with(chosen_rule, max_total_notional=Decimal("120"))
    assert planner.evaluate_rule(context(), chosen_rule).reason is ReasonCode.MAX_TOTAL_NOTIONAL


def test_total_leverage_limit() -> None:
    chosen_rule = rule(
        trigger=TriggerKind.ALWAYS,
        trigger_value="0",
        quantity_kind=QuantityKind.FIXED,
        quantity_value="10",
    )
    planner = planner_with(chosen_rule, max_total_leverage=Decimal("0.05"))
    assert planner.evaluate_rule(context(), chosen_rule).reason is ReasonCode.MAX_TOTAL_LEVERAGE


def test_additional_risk_limit() -> None:
    chosen_rule = rule(trigger=TriggerKind.ALWAYS, trigger_value="0", quantity_value="1")
    planner = planner_with(chosen_rule, max_additional_risk_fraction=Decimal("0.0001"))
    assert planner.evaluate_rule(context(), chosen_rule).reason is ReasonCode.MAX_ADDITIONAL_RISK


def test_average_price_deterioration_limit() -> None:
    chosen_rule = rule(
        mode=ScalingMode.PYRAMIDING,
        trigger=TriggerKind.ALWAYS,
        trigger_value="0",
        quantity_kind=QuantityKind.FIXED,
        quantity_value="10",
    )
    planner = planner_with(
        chosen_rule,
        max_total_leverage=Decimal("100"),
        max_total_position_risk_fraction=Decimal("1"),
        max_additional_risk_fraction=Decimal("1"),
        maximum_average_price_deterioration_fraction=Decimal("0.001"),
    )
    result = planner.evaluate_rule(context(current_price=Decimal("110")), chosen_rule)
    assert result.reason is ReasonCode.AVERAGE_PRICE_DETERIORATION


def test_risk_request_contains_current_portfolio_and_adjustment_identity() -> None:
    chosen_rule = rule()
    planner = planner_with(chosen_rule)
    ctx = context()
    plan = planner.evaluate_rule(ctx, chosen_rule).plan
    assert plan is not None
    request = planner.build_risk_request(
        ctx,
        plan,
        {"quantity_step": "0.001", "contract_size": "1"},
    )
    assert request["request_id"] == plan.risk_request_id
    assert request["metadata"]["adjustment_id"] == plan.adjustment_id
    assert request["portfolio"]["open_risk_amount"] == "5"


def test_risk_request_rejects_reduction_plan() -> None:
    scale_rule = rule(
        mode=ScalingMode.SCALE_OUT,
        action=AdjustmentAction.REDUCE,
        trigger=TriggerKind.ALWAYS,
        trigger_value="0",
        quantity_kind=QuantityKind.CURRENT_FRACTION,
    )
    planner = planner_with(scale_rule)
    ctx = context()
    plan = planner.evaluate_rule(ctx, scale_rule).plan
    assert plan is not None
    with pytest.raises(ValueError):
        planner.build_risk_request(ctx, plan, {})
