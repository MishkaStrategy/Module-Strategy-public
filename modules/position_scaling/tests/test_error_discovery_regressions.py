from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from conftest import feature, make_position, make_view, standard_features

from modular_strategy_position_scaling.canonical import deep_thaw
from modular_strategy_position_scaling.decimal_utils import canonical_decimal
from modular_strategy_position_scaling.engine import ScalingContext, ScalingPlanner
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    QuantityKind,
    ReasonCode,
    ScalingMode,
    TriggerKind,
)
from modular_strategy_position_scaling.models import PositionAdjustmentPlan, ScalingRule
from modular_strategy_position_scaling.module import (
    FEATURE_APPROVED,
    FEATURE_PLAN,
    FEATURE_REJECTED,
    FEATURE_RISK_DECISION,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)


def _averaging_module() -> PositionScalingPlannerModule:
    return PositionScalingPlannerModule(
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
                "quantity_value": "0.4",
                "expires_bars": 5,
                "cooldown_bars": 1,
            }
        ],
    )


def _plan(module: PositionScalingPlannerModule, *, index: int = 0) -> Any:
    decision = module.on_bar(
        make_view(
            index=index,
            close=98,
            position=make_position(pnl=-20),
            features=standard_features(index),
        )
    )
    return next(item for item in decision.features if item.key == FEATURE_PLAN)


def _risk_payload(
    plan: Mapping[str, Any],
    *,
    quantity: str = "0.2",
    approved: bool = True,
    setup_id: str | None | object = object(),
    schema_version: str = "1",
) -> dict[str, Any]:
    actual_setup = plan["setup_id"] if type(setup_id) is object else setup_id
    return {
        "schema_version": schema_version,
        "request_id": plan["risk_request_id"],
        "symbol": plan["symbol"],
        "status": "approved" if approved else "rejected",
        "approved": approved,
        "quantity": quantity if approved else "0",
        "raw_quantity": quantity if approved else "0",
        "risk_fraction": "0.001",
        "risk_budget": "10",
        "actual_risk_amount": "1" if approved else "0",
        "actual_risk_fraction": "0.0001" if approved else "0",
        "notional": "19.6" if approved else "0",
        "required_leverage": "0.002" if approved else "0",
        "binding_constraints": [],
        "reject_reason": None if approved else "no_risk_capacity",
        "message": "" if approved else "capacity exhausted",
        "setup_id": actual_setup,
        "diagnostics": {},
    }


def _approval_feature(plan_feature: Any, *, quantity: str = "0.2") -> Any:
    risk = feature(
        FEATURE_RISK_DECISION,
        _risk_payload(plan_feature.value, quantity=quantity),
        source="risk_sizing",
    )
    result = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan_feature, risk))
    )
    return next(item for item in result.features if item.key == FEATURE_APPROVED)


def test_partial_risk_approval_reconciles_approved_total() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    approved = _approval_feature(plan, quantity="0.2")

    planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=1.2, entry_price=99.6666666667, entry_fill_count=2),
            features=(*standard_features(1), approved),
        )
    )
    state = planner.snapshot_state()
    assert state["completed_stages"] == [1]
    assert state["scale_in_count"] == 1
    assert state["pending_plan"] is None
    assert state["lifecycle"] == "reconciled"


def test_partial_approval_survives_checkpoint_before_fill() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    approved = _approval_feature(plan, quantity="0.2")
    planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=(*standard_features(1), approved),
        )
    )
    snapshot = planner.snapshot_state()
    assert snapshot["pending_approved_quantity"] == "0.2"

    restored = _averaging_module()
    restored.restore_state(snapshot)
    restored.on_bar(
        make_view(
            index=2,
            close=98,
            position=make_position(quantity=1.2, entry_price=99.6666666667, entry_fill_count=2),
            features=standard_features(2),
        )
    )
    state = restored.snapshot_state()
    assert state["completed_stages"] == [1]
    assert state["pending_plan"] is None


def test_adjustment_overfill_enters_recovery_instead_of_completing() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    approved = _approval_feature(plan, quantity="0.4")
    result = planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=1.5, entry_price=99.3, entry_fill_count=2),
            features=(*standard_features(1), approved),
        )
    )
    assert result.skips == (ReasonCode.ADJUSTMENT_OVERFILL.value,)
    state = planner.snapshot_state()
    assert state["lifecycle"] == "recovery_required"
    assert state["completed_stages"] == []
    assert state["scale_in_count"] == 0


def test_full_scale_out_fails_closed_without_exception() -> None:
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
                "action": "reduce",
                "trigger_kind": "always",
                "trigger_value": 0,
                "quantity_kind": "current_fraction",
                "quantity_value": 1,
                "expires_bars": 0,
            }
        ],
    )
    result = module.on_bar(make_view(position=make_position()))
    assert result.skips == (ReasonCode.INVALID_QUANTITY.value,)
    assert FEATURE_PLAN not in {item.key for item in result.features}


def _context(*, scale_out_count: int = 0, data_gap: bool = True) -> ScalingContext:
    return ScalingContext(
        position_id="p",
        setup_id="s",
        symbol="BTCUSDT",
        side="long",
        quantity=Decimal("1"),
        average_price=Decimal("100"),
        initial_quantity=Decimal("1"),
        initial_entry_price=Decimal("100"),
        initial_stop_price=Decimal("95"),
        stop_price=Decimal("95"),
        current_price=Decimal("98"),
        unrealized_pnl=Decimal("-2"),
        equity=Decimal("10000"),
        cash=Decimal("9000"),
        index=2,
        attached_index=0,
        decision_time=datetime(2026, 1, 1, tzinfo=UTC),
        bar_seconds=60,
        last_adjustment_index=None,
        last_adjustment_price=None,
        scale_in_count=0,
        scale_out_count=scale_out_count,
        data_gap=data_gap,
        exit_pending=False,
        pending_order_count=0,
        setup_valid=False,
        market_regime=None,
        atr=None,
        liquidation_distance_fraction=None,
        margin_usage_fraction=None,
        available_margin=Decimal("9000"),
        source_feature_ids=(),
        features={},
    )


def _rebalance_rule() -> ScalingRule:
    return ScalingRule(
        stage=1,
        mode=ScalingMode.REBALANCE,
        action=AdjustmentAction.REBALANCE,
        trigger_kind=TriggerKind.ALWAYS,
        trigger_value=Decimal("0"),
        quantity_kind=QuantityKind.TARGET_QUANTITY,
        quantity_value=Decimal("0.5"),
        expires_bars=0,
    )


def test_rebalance_decrease_uses_reduction_safety_path() -> None:
    rule = _rebalance_rule()
    config = PositionScalingPlannerModule(
        enabled=True,
        allow_rebalance=True,
        max_scale_out_count=1,
        rules=[rule.to_mapping()],
    ).config
    result = ScalingPlanner(config).evaluate_rule(_context(), rule)
    assert result.plan is not None
    assert result.plan.direction is AdjustmentDirection.DECREASE

    blocked = ScalingPlanner(config).evaluate_rule(_context(scale_out_count=1), rule)
    assert blocked.plan is None
    assert blocked.reason is ReasonCode.MAX_SCALE_OUT_COUNT


def test_plan_rejects_action_direction_and_geometry_mismatch() -> None:
    raw = deep_thaw(_plan(_averaging_module()).value)
    raw["direction"] = "decrease"
    with pytest.raises(ValueError, match="action and direction"):
        PositionAdjustmentPlan.from_mapping(raw)

    raw = deep_thaw(_plan(_averaging_module()).value)
    raw["expected_total_quantity"] = "999"
    with pytest.raises(ValueError, match="expected_total_quantity"):
        PositionAdjustmentPlan.from_mapping(raw)

    raw = deep_thaw(_plan(_averaging_module()).value)
    raw["expected_average_price"] = "1"
    with pytest.raises(ValueError, match="expected_average_price"):
        PositionAdjustmentPlan.from_mapping(raw)


def test_approval_rejects_wrong_risk_schema_and_missing_setup() -> None:
    plan = _plan(_averaging_module())
    wrong_schema = feature(
        FEATURE_RISK_DECISION,
        _risk_payload(plan.value, schema_version="2"),
        source="risk_sizing",
    )
    result = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, wrong_schema))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.RISK_DECISION_STALE.value

    missing_setup = feature(
        FEATURE_RISK_DECISION,
        _risk_payload(plan.value, setup_id=None),
        source="risk_sizing",
        feature_id="missing-setup",
    )
    result = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, missing_setup))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.RISK_DECISION_MISMATCH.value


def test_position_setup_identity_change_is_rejected() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    risk = feature(
        FEATURE_RISK_DECISION,
        _risk_payload(plan.value),
        source="risk_sizing",
    )
    result = PositionScalingApprovalModule().on_bar(
        make_view(
            close=98,
            position=make_position(setup_id="replacement-setup"),
            features=(plan, risk),
        )
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.POSITION_SNAPSHOT_MISMATCH.value

    planner_result = planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(setup_id="replacement-setup"),
            features=standard_features(1),
        )
    )
    assert planner_result.skips == (ReasonCode.POSITION_CHANGED.value,)


def test_risk_rejection_feedback_clears_pending_plan() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    rejected_risk = feature(
        FEATURE_RISK_DECISION,
        _risk_payload(plan.value, approved=False),
        source="risk_sizing",
    )
    approval_result = PositionScalingApprovalModule().on_bar(
        make_view(close=98, position=make_position(), features=(plan, rejected_risk))
    )
    rejected = next(item for item in approval_result.features if item.key == FEATURE_REJECTED)
    planner_result = planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(),
            features=(*standard_features(1), rejected),
        )
    )
    assert planner_result.skips == (ReasonCode.RISK_REJECTED.value,)
    state = planner.snapshot_state()
    assert state["pending_plan"] is None
    assert state["lifecycle"] == "rejected"


def test_approval_state_schema_two_migrates_to_current() -> None:
    module = PositionScalingApprovalModule(max_processed_adjustments=2)
    state = dict(module.snapshot_state())
    state["schema_version"] = "2"
    state.pop("state_fingerprint", None)
    restored = PositionScalingApprovalModule(max_processed_adjustments=2)
    restored.restore_state(state)
    assert restored.snapshot_state()["schema_version"] == "4"


def test_canonical_decimal_preserves_more_than_context_precision() -> None:
    value = Decimal("100.0099009900990099009900990099010")
    encoded = canonical_decimal(value)
    assert encoded == "100.009900990099009900990099009901"
    assert Decimal(encoded) == value


def test_exact_duplicate_approval_feedback_is_idempotent() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    approved = _approval_feature(plan, quantity="0.2")
    duplicate = feature(
        FEATURE_APPROVED,
        deep_thaw(approved.value),
        index=1,
        source="position_scaling_approval",
        feature_id="duplicate-approval",
    )
    view = make_view(
        index=1,
        close=98,
        position=make_position(),
        features=(*standard_features(1), approved, duplicate),
    )
    planner.on_bar(view)
    history_after_first = planner.snapshot_state()["history"]
    planner.on_bar(view)
    history_after_second = planner.snapshot_state()["history"]
    approved_rows = [row for row in history_after_second if row.get("event") == "approved"]
    assert len(approved_rows) == 1
    assert history_after_second == history_after_first


def test_newer_unrelated_feedback_does_not_mask_pending_approval() -> None:
    planner = _averaging_module()
    plan = _plan(planner)
    approved = _approval_feature(plan, quantity="0.2")
    unrelated_value = deep_thaw(approved.value)
    unrelated_value["adjustment_id"] = "unrelated-adjustment"
    unrelated = feature(
        FEATURE_APPROVED,
        unrelated_value,
        index=1,
        source="position_scaling_approval",
        feature_id="zz-unrelated-approval",
    )
    planner.on_bar(
        make_view(
            index=1,
            close=98,
            position=make_position(quantity=1.2, entry_price=99.6666666667),
            features=(*standard_features(1), approved, unrelated),
        )
    )
    assert planner.snapshot_state()["completed_stages"] == [1]


def test_legacy_pending_plan_repairs_truncated_average_on_restore() -> None:
    from decimal import localcontext

    from modular_strategy_position_scaling.canonical import fingerprint

    planner = _averaging_module()
    _plan(planner)
    snapshot = dict(planner.snapshot_state())
    legacy_plan = dict(snapshot["pending_plan"])
    with localcontext() as context:
        context.prec = 28
        legacy_plan["expected_average_price"] = format(
            Decimal(str(legacy_plan["expected_average_price"])).normalize(), "f"
        )
    snapshot["schema_version"] = "2"
    snapshot.pop("state_fingerprint", None)
    snapshot.pop("state_fingerprint", None)
    snapshot["pending_plan"] = legacy_plan
    snapshot["pending_fingerprint"] = fingerprint(legacy_plan)
    snapshot.pop("pending_approved_quantity", None)
    snapshot.pop("pending_feedback_fingerprint", None)

    restored = _averaging_module()
    restored.restore_state(snapshot)
    migrated = restored.snapshot_state()
    assert migrated["schema_version"] == "4"
    assert (
        migrated["pending_plan"]["expected_average_price"] != legacy_plan["expected_average_price"]
    )
    PositionAdjustmentPlan.from_mapping(migrated["pending_plan"])
