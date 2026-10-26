from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from conftest import feature, make_position, make_view, standard_features

from modular_strategy_position_scaling.canonical import fingerprint
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    QuantityKind,
    ReasonCode,
    ScalingMode,
    TriggerKind,
)
from modular_strategy_position_scaling.models import (
    ApprovedAdjustment,
    PositionAdjustmentPlan,
    RejectedAdjustment,
    ScalingRule,
)
from modular_strategy_position_scaling.module import (
    FEATURE_PLAN,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)
from modular_strategy_position_scaling.state import PlannerState


def valid_plan() -> PositionAdjustmentPlan:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    return PositionAdjustmentPlan(
        adjustment_id="a",
        position_id="p",
        setup_id="s",
        symbol="BTCUSDT",
        side="long",
        action=AdjustmentAction.ADD,
        direction=AdjustmentDirection.INCREASE,
        scaling_mode=ScalingMode.AVERAGING_DOWN,
        stage=1,
        decision_time=now,
        decision_index=0,
        desired_entry_kind="limit",
        desired_price=Decimal("98"),
        trigger_price=Decimal("98"),
        desired_quantity=Decimal("0.1"),
        maximum_quantity=Decimal("0.1"),
        risk_budget=Decimal("10"),
        notional_budget=Decimal("1000"),
        current_average_price=Decimal("100"),
        expected_average_price=Decimal("99.81818181818181818181818181818182"),
        current_quantity=Decimal("1"),
        expected_total_quantity=Decimal("1.1"),
        current_total_risk=Decimal("5"),
        projected_total_risk=Decimal("5.29"),
        maximum_total_risk=Decimal("50"),
        stop_price=Decimal("95"),
        invalidation_price=Decimal("95"),
        expires_at=now,
        expires_index=0,
        reason_code=ReasonCode.RISK_PENDING,
        risk_request_id="r",
        metadata={},
        source_feature_ids=(),
        config_fingerprint="f",
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": ScalingMode.REBALANCE, "action": AdjustmentAction.ADD},
        {"mode": ScalingMode.AVERAGING_DOWN, "action": AdjustmentAction.REBALANCE},
        {
            "mode": ScalingMode.REBALANCE,
            "action": AdjustmentAction.REBALANCE,
            "quantity_kind": QuantityKind.FIXED,
        },
        {
            "mode": ScalingMode.AVERAGING_DOWN,
            "action": AdjustmentAction.ADD,
            "quantity_kind": QuantityKind.TARGET_QUANTITY,
        },
        {"trigger_kind": TriggerKind.BARS, "trigger_value": Decimal("1.5")},
        {
            "quantity_kind": QuantityKind.INITIAL_FRACTION,
            "quantity_value": Decimal("1.1"),
        },
    ],
)
def test_more_rule_invariants(kwargs: dict[str, Any]) -> None:
    values: dict[str, Any] = {
        "stage": 1,
        "mode": ScalingMode.AVERAGING_DOWN,
        "action": AdjustmentAction.ADD,
        "trigger_kind": TriggerKind.ALWAYS,
        "trigger_value": Decimal("0"),
        "quantity_kind": QuantityKind.FIXED,
        "quantity_value": Decimal("1"),
    }
    values.update(kwargs)
    with pytest.raises(ValueError):
        ScalingRule(**values)


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        ("side", "bad", ValueError),
        ("action", "add", TypeError),
        ("stage", True, TypeError),
        ("stage", 0, ValueError),
        ("schema_version", "2", ValueError),
        ("maximum_quantity", Decimal("0.05"), ValueError),
        ("risk_request_id", None, ValueError),
        ("setup_id", " ", ValueError),
        ("adjustment_id", " a", ValueError),
    ],
)
def test_more_plan_invariants(field: str, value: object, error: type[Exception]) -> None:
    plan = valid_plan()
    values = {name: getattr(plan, name) for name in plan.__dataclass_fields__}
    values[field] = value
    with pytest.raises(error):
        PositionAdjustmentPlan(**values)


def test_plan_time_and_direction_invariants() -> None:
    plan = valid_plan()
    values = {name: getattr(plan, name) for name in plan.__dataclass_fields__}
    values["expires_at"] = plan.decision_time - timedelta(seconds=1)
    with pytest.raises(ValueError):
        PositionAdjustmentPlan(**values)
    values = {name: getattr(plan, name) for name in plan.__dataclass_fields__}
    values.update(
        direction=AdjustmentDirection.NONE,
        desired_quantity=Decimal("0.1"),
        risk_request_id=None,
        expected_total_quantity=plan.current_quantity,
    )
    with pytest.raises(ValueError):
        PositionAdjustmentPlan(**values)
    values = {name: getattr(plan, name) for name in plan.__dataclass_fields__}
    values.update(
        direction=AdjustmentDirection.DECREASE,
        action=AdjustmentAction.REDUCE,
        scaling_mode=ScalingMode.SCALE_OUT,
        risk_request_id=None,
        expected_total_quantity=plan.current_quantity,
    )
    with pytest.raises(ValueError):
        PositionAdjustmentPlan(**values)


def test_approved_and_rejected_additional_invariants() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    base = dict(
        adjustment_id="a",
        position_id="p",
        symbol="BTCUSDT",
        side="long",
        action=AdjustmentAction.ADD,
        direction=AdjustmentDirection.INCREASE,
        approved_quantity=Decimal("1"),
        desired_price=Decimal("100"),
        risk_request_id="r",
        reason_code=ReasonCode.RISK_APPROVED,
        approved_at=now,
        plan={},
    )
    for changes in (
        {"schema_version": "2"},
        {"side": "bad"},
        {"action": "add"},
        {"approved_quantity": Decimal("0")},
    ):
        with pytest.raises((TypeError, ValueError)):
            ApprovedAdjustment(**{**base, **changes})
    reject = dict(
        adjustment_id="a",
        position_id="p",
        symbol="BTCUSDT",
        reason_code=ReasonCode.RISK_REJECTED,
        message="bad",
        rejected_at=now,
    )
    with pytest.raises(ValueError):
        RejectedAdjustment(**reject, schema_version="2")
    with pytest.raises(TypeError):
        RejectedAdjustment(**{**reject, "reason_code": "risk_rejected"})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed_stages", "bad"),
        ("counters", []),
        ("pending_plan", []),
        ("processed_event_ids", "bad"),
        ("history", "bad"),
    ],
)
def test_state_more_malformed_fields(field: str, value: object) -> None:
    state = PlannerState(config_fingerprint="x")
    payload = dict(state.snapshot())
    payload[field] = value
    with pytest.raises(ValueError):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )


def test_state_pending_fingerprint_invariants() -> None:
    plan = valid_plan().to_mapping()
    state = PlannerState(config_fingerprint="x")
    payload = dict(state.snapshot())
    payload.update(
        position_id="p",
        setup_id="s",
        symbol="BTCUSDT",
        side="long",
        initial_quantity="1",
        initial_entry_price="100",
        initial_stop_price="95",
        last_quantity="1",
        last_entry_price="100",
        pending_plan=plan,
        pending_fingerprint="wrong",
    )
    with pytest.raises(ValueError, match="fingerprint"):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )
    payload = dict(state.snapshot())
    payload["pending_fingerprint"] = "orphan"
    payload["state_fingerprint"] = fingerprint(
        {key: value for key, value in payload.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="requires"):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )


def test_approval_restore_more_paths() -> None:
    module = PositionScalingApprovalModule(max_processed_adjustments=1)
    module.restore_state({})
    bad = dict(module.snapshot_state())
    bad["schema_version"] = "99"
    with pytest.raises(ValueError):
        module.restore_state(bad)
    bad = dict(module.snapshot_state())
    bad["fingerprints"] = []
    with pytest.raises(ValueError):
        module.restore_state(bad)
    bad = dict(module.snapshot_state())
    bad["order"] = ["a", "a"]
    with pytest.raises(ValueError):
        module.restore_state(bad)
    bad = dict(module.snapshot_state())
    bad["fingerprints"] = {"a": "x"}
    with pytest.raises(ValueError):
        module.restore_state(bad)
    bad = dict(module.snapshot_state())
    bad["last_index"] = True
    with pytest.raises(ValueError):
        module.restore_state(bad)


def test_extract_helpers_via_feature_shapes() -> None:
    module = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        require_market_regime=True,
        allowed_market_regimes=["trend"],
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    )
    features = list(standard_features())
    features = [
        item
        for item in features
        if item.key not in {"setup.validity", "market_regime.state", "market.atr"}
    ]
    features.extend(
        [
            feature("setup.validity", True),
            feature("market_regime.state", "trend"),
            feature("market.atr", "bad"),
        ]
    )
    decision = module.on_bar(make_view(close=98, position=make_position(), features=features))
    assert FEATURE_PLAN in {item.key for item in decision.features}
