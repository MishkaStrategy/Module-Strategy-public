from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any

import pytest
from conftest import feature, make_position, make_view, standard_features

from modular_strategy_position_scaling.canonical import canonical_value, deep_thaw, fingerprint
from modular_strategy_position_scaling.config import PositionScalingConfig
from modular_strategy_position_scaling.engine import ScalingContext
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
    FEATURE_REJECTED,
    FEATURE_RISK_DECISION,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)
from modular_strategy_position_scaling.state import PlannerState


class SampleEnum(StrEnum):
    A = "a"


def base_plan_mapping() -> dict[str, Any]:
    module = PositionScalingPlannerModule(
        enabled=True,
        allow_averaging_down=True,
        rules=[
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "adverse_percent",
                "trigger_value": "0.01",
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
                "expires_bars": 0,
            }
        ],
    )
    decision = module.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )
    return dict(next(item.value for item in decision.features if item.key == FEATURE_PLAN))


def test_canonical_rejects_unsupported_object() -> None:
    with pytest.raises(TypeError):
        canonical_value(object())


def test_deep_thaw_handles_all_supported_containers() -> None:
    value = MappingProxyType(
        {
            "tuple": (Decimal("1.0"), SampleEnum.A),
            "set": frozenset({2, 1}),
            "time": datetime(2026, 1, 1, tzinfo=UTC),
        }
    )
    result = deep_thaw(value)
    assert result["tuple"] == ["1", "a"]
    assert result["set"] == [1, 2]
    assert result["time"].startswith("2026-01-01")


@pytest.mark.parametrize(
    "parameters",
    [
        {"max_scale_in_count": -1},
        {"max_history": 0},
        {"max_total_leverage": 0},
        {"max_total_position_risk_fraction": "1.1"},
        {"max_additional_risk_fraction": "0.6", "max_total_position_risk_fraction": "0.5"},
        {"allowed_market_regimes": "trend"},
        {"setup_validity_feature": "bad"},
        {"instrument_rules": "bad"},
    ],
)
def test_config_adversarial_values_rejected(parameters: Mapping[str, Any]) -> None:
    with pytest.raises((TypeError, ValueError)):
        PositionScalingConfig.from_mapping(parameters)


def test_rule_unknown_field_rejected() -> None:
    with pytest.raises(ValueError, match="unknown scaling rule"):
        PositionScalingConfig.from_mapping(
            {
                "enabled": True,
                "allow_scale_out": True,
                "rules": [
                    {
                        "stage": 1,
                        "mode": "scale_out",
                        "action": "reduce",
                        "trigger_kind": "always",
                        "trigger_value": 0,
                        "quantity_kind": "fixed",
                        "quantity_value": 1,
                        "future": 1,
                    }
                ],
            }
        )


def test_rule_metadata_must_be_mapping() -> None:
    with pytest.raises(TypeError, match="metadata"):
        PositionScalingConfig.from_mapping(
            {
                "enabled": True,
                "allow_scale_out": True,
                "rules": [
                    {
                        "stage": 1,
                        "mode": "scale_out",
                        "action": "reduce",
                        "trigger_kind": "always",
                        "trigger_value": 0,
                        "quantity_kind": "fixed",
                        "quantity_value": 1,
                        "metadata": [],
                    }
                ],
            }
        )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"stage": True},
        {"trigger_kind": TriggerKind.FEATURE_TRUE, "feature_key": None},
        {"trigger_kind": TriggerKind.PRICE_LADDER, "explicit_price": None},
        {"action": AdjustmentAction.ADD, "mode": ScalingMode.SCALE_OUT},
        {"action": AdjustmentAction.REDUCE, "mode": ScalingMode.PYRAMIDING},
        {"cooldown_bars": -1},
        {"desired_entry_kind": " "},
    ],
)
def test_scaling_rule_invariants(kwargs: Mapping[str, Any]) -> None:
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
    with pytest.raises((TypeError, ValueError)):
        ScalingRule(**values)


def test_plan_rejects_naive_timestamp() -> None:
    payload = base_plan_mapping()
    payload["decision_time"] = "2026-01-01T00:00:00"
    with pytest.raises(ValueError, match="timezone"):
        PositionAdjustmentPlan.from_mapping(payload)


def test_plan_rejects_bad_expiration_and_direction() -> None:
    payload = base_plan_mapping()
    payload["expires_index"] = -1
    with pytest.raises(ValueError):
        PositionAdjustmentPlan.from_mapping(payload)
    payload = base_plan_mapping()
    payload["direction"] = "decrease"
    with pytest.raises(ValueError):
        PositionAdjustmentPlan.from_mapping(payload)


def test_plan_rejects_bad_metadata_and_source_ids() -> None:
    payload = base_plan_mapping()
    payload["metadata"] = []
    with pytest.raises(ValueError):
        PositionAdjustmentPlan.from_mapping(payload)
    payload = base_plan_mapping()
    payload["source_feature_ids"] = "bad"
    with pytest.raises(ValueError):
        PositionAdjustmentPlan.from_mapping(payload)


def test_approved_and_rejected_dto_validation() -> None:
    with pytest.raises(ValueError):
        ApprovedAdjustment(
            adjustment_id="",
            position_id="p",
            symbol="BTCUSDT",
            side="long",
            action=AdjustmentAction.ADD,
            direction=AdjustmentDirection.INCREASE,
            approved_quantity=Decimal("1"),
            desired_price=Decimal("100"),
            risk_request_id="r",
            reason_code=ReasonCode.RISK_APPROVED,
            approved_at=datetime.now(UTC),
            plan={},
        )
    with pytest.raises(ValueError):
        RejectedAdjustment(
            adjustment_id="a",
            position_id="p",
            symbol="BTCUSDT",
            reason_code=ReasonCode.RISK_REJECTED,
            message="",
            rejected_at=datetime.now(UTC),
        )


def test_scaling_context_properties_cover_zero_risk_and_short_drawdown() -> None:
    ctx = ScalingContext(
        position_id="p",
        setup_id=None,
        symbol="BTCUSDT",
        side="short",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("100"),
        stop_price=Decimal("105"),
        current_price=Decimal("90"),
        unrealized_pnl=Decimal("-100"),
        equity=Decimal("1000"),
        cash=Decimal("900"),
        index=5,
        attached_index=10,
        decision_time=datetime.now(UTC),
        bar_seconds=60,
        last_adjustment_index=None,
        last_adjustment_price=None,
        scale_in_count=0,
        scale_out_count=0,
        data_gap=False,
        exit_pending=False,
        pending_order_count=0,
        setup_valid=True,
        market_regime=None,
        atr=None,
        liquidation_distance_fraction=None,
        margin_usage_fraction=None,
        available_margin=None,
        source_feature_ids=(),
        features={},
    )
    assert ctx.current_r_multiple == 0
    assert ctx.drawdown_fraction == Decimal("0.1")
    assert ctx.bars_held == 0


def test_attach_rejects_missing_stop() -> None:
    module = PositionScalingPlannerModule(enabled=True)
    decision = module.on_bar(make_view(position=make_position(stop_loss=None)))
    assert decision.skips == (ReasonCode.STOP_INVALID.value,)


def test_required_market_regime_blocks_missing_and_disallowed() -> None:
    params = {
        "enabled": True,
        "allow_averaging_down": True,
        "require_market_regime": True,
        "allowed_market_regimes": ["trend"],
        "rules": [
            {
                "stage": 1,
                "mode": "averaging_down",
                "action": "add",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": "0.1",
            }
        ],
    }
    module = PositionScalingPlannerModule(**params)
    features = tuple(item for item in standard_features() if item.key != "market_regime.state")
    assert module.on_bar(
        make_view(close=98, position=make_position(), features=features)
    ).skips == (ReasonCode.MARKET_REGIME_BLOCKED.value,)


def test_stop_scaling_sets_stopped_lifecycle() -> None:
    module = PositionScalingPlannerModule(
        enabled=True,
        allow_scale_out=True,
        require_setup_validity=False,
        require_liquidation_distance=False,
        require_margin_usage=False,
        rules=[
            {
                "stage": 1,
                "mode": "scale_out",
                "action": "stop_scaling",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "fixed",
                "quantity_value": 1,
                "expires_bars": 0,
            }
        ],
    )
    decision = module.on_bar(make_view(position=make_position()))
    assert FEATURE_PLAN in {item.key for item in decision.features}
    assert module.snapshot_state()["lifecycle"] == "stopped"


def test_approval_rejects_invalid_plan_payload() -> None:
    approval = PositionScalingApprovalModule()
    bad = feature(FEATURE_PLAN, {"bad": 1})
    assert approval.on_bar(make_view(features=(bad,))).skips == (ReasonCode.INVALID_RULE.value,)


def test_approval_rejects_stale_risk_and_symbol_mismatch() -> None:
    plan = base_plan_mapping()
    plan_feature = feature(FEATURE_PLAN, plan)
    stale_risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan["risk_request_id"],
            "symbol": "BTCUSDT",
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan["setup_id"],
        },
        index=-1,
    )
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(position=make_position(), features=(plan_feature, stale_risk))
    )
    assert result.skips == (ReasonCode.RISK_DECISION_MISSING.value,) or FEATURE_REJECTED in {
        item.key for item in result.features
    }

    approval = PositionScalingApprovalModule()
    wrong_symbol = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan["risk_request_id"],
            "symbol": "ETHUSDT",
            "status": "approved",
            "approved": True,
            "quantity": "0.1",
            "setup_id": plan["setup_id"],
        },
    )
    result = approval.on_bar(
        make_view(position=make_position(), features=(plan_feature, wrong_symbol))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.RISK_DECISION_MISMATCH.value


def test_approval_rejects_zero_approved_quantity() -> None:
    plan = base_plan_mapping()
    plan_feature = feature(FEATURE_PLAN, plan)
    risk = feature(
        FEATURE_RISK_DECISION,
        {
            "schema_version": "1",
            "request_id": plan["risk_request_id"],
            "symbol": "BTCUSDT",
            "status": "approved",
            "approved": True,
            "quantity": "0",
            "setup_id": plan["setup_id"],
        },
    )
    result = PositionScalingApprovalModule().on_bar(
        make_view(position=make_position(), features=(plan_feature, risk))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.RISK_REJECTED.value


def test_approval_restore_rejects_config_and_counter_corruption() -> None:
    module = PositionScalingApprovalModule(max_processed_adjustments=2)
    state = dict(module.snapshot_state())
    state["max_processed_adjustments"] = 3
    state["state_fingerprint"] = fingerprint(
        {key: value for key, value in state.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="configuration"):
        module.restore_state(state)
    state = dict(module.snapshot_state())
    state["counters"] = {"received": -1}
    state["state_fingerprint"] = fingerprint(
        {key: value for key, value in state.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="counter"):
        module.restore_state(state)


def test_planner_state_restore_rejects_unknown_and_duplicate_events() -> None:
    state = PlannerState(config_fingerprint="x")
    payload = dict(state.snapshot())
    payload["unknown"] = 1
    with pytest.raises(ValueError, match="unknown"):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )
    payload = dict(state.snapshot())
    payload["processed_event_ids"] = ["a", "a"]
    payload["state_fingerprint"] = fingerprint(
        {key: value for key, value in payload.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="duplicates"):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )


def test_planner_state_restore_rejects_inconsistent_flat_and_open_states() -> None:
    state = PlannerState(config_fingerprint="x")
    payload = dict(state.snapshot())
    payload["initial_quantity"] = "1"
    payload["state_fingerprint"] = fingerprint(
        {key: value for key, value in payload.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="flat state"):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )
    payload = dict(state.snapshot())
    payload["position_id"] = "p"
    payload["symbol"] = "BTCUSDT"
    payload["side"] = "long"
    payload["state_fingerprint"] = fingerprint(
        {key: value for key, value in payload.items() if key != "state_fingerprint"}
    )
    with pytest.raises(ValueError, match="requires positive"):
        PlannerState.restore(
            payload,
            config_fingerprint="x",
            max_history=2,
            max_processed_events=2,
        )
