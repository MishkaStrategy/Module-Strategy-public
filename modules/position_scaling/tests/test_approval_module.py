from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest
from conftest import feature, make_position, make_view, standard_features

from modular_strategy_position_scaling import (
    FEATURE_APPROVED,
    FEATURE_PLAN,
    FEATURE_REJECTED,
    FEATURE_RISK_DECISION,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)
from modular_strategy_position_scaling.canonical import deep_thaw
from modular_strategy_position_scaling.enums import ReasonCode


def planner_decision(parameters: Mapping[str, Any]) -> Any:
    planner = PositionScalingPlannerModule(**parameters)
    return planner.on_bar(
        make_view(close=98, position=make_position(), features=standard_features())
    )


def plan_feature(parameters: Mapping[str, Any]) -> Any:
    return next(item for item in planner_decision(parameters).features if item.key == FEATURE_PLAN)


def risk_decision_for(plan: Mapping[str, Any], **overrides: object) -> Mapping[str, Any]:
    payload: dict[str, object] = {
        "schema_version": "1",
        "request_id": plan["risk_request_id"],
        "symbol": plan["symbol"],
        "status": "approved",
        "approved": True,
        "quantity": "0.2",
        "raw_quantity": "0.2",
        "risk_fraction": "0.001",
        "risk_budget": "10",
        "actual_risk_amount": "1",
        "actual_risk_fraction": "0.0001",
        "notional": "19.6",
        "required_leverage": "0.002",
        "binding_constraints": [],
        "reject_reason": None,
        "message": "",
        "setup_id": plan["setup_id"],
        "diagnostics": {},
    }
    payload.update(overrides)
    return payload


def test_missing_risk_decision_remains_pending_and_can_later_approve(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    approval = PositionScalingApprovalModule()
    first = approval.on_bar(make_view(close=98, position=make_position(), features=(plan,)))
    assert first.skips == (ReasonCode.RISK_DECISION_MISSING.value,)
    decision_feature = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value),
        source="risk_sizing",
    )
    second = approval.on_bar(
        make_view(close=98, position=make_position(), features=(plan, decision_feature))
    )
    assert FEATURE_APPROVED in {item.key for item in second.features}


def test_risk_approval_caps_quantity_to_plan(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    decision_feature = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value, quantity="10"),
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(close=98, position=make_position(), features=(plan, decision_feature))
    )
    approved = next(item.value for item in result.features if item.key == FEATURE_APPROVED)
    assert approved["approved_quantity"] == plan.value["desired_quantity"]


def test_partial_risk_approval_reduces_quantity(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    decision_feature = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value, quantity="0.1"),
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(close=98, position=make_position(), features=(plan, decision_feature))
    )
    approved = next(item.value for item in result.features if item.key == FEATURE_APPROVED)
    assert approved["approved_quantity"] == "0.1"


def test_risk_rejection_publishes_typed_rejection(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    decision_feature = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(
            plan.value,
            status="rejected",
            approved=False,
            quantity="0",
            reject_reason="no_risk_capacity",
            message="capacity exhausted",
        ),
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(close=98, position=make_position(), features=(plan, decision_feature))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.RISK_REJECTED.value
    assert rejected["message"] == "capacity exhausted"


def test_risk_request_identity_mismatch_rejected(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    decision_feature = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value, request_id="other"),
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(close=98, position=make_position(), features=(plan, decision_feature))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.RISK_DECISION_MISMATCH.value


def test_stale_plan_rejected(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(
        make_view(index=1, close=98, position=make_position(), features=(plan,))
    )
    assert (
        ReasonCode.NO_TRIGGER.value in result.skips or ReasonCode.EXPIRED_PLAN.value in result.skips
    )


def test_duplicate_terminal_plan_is_idempotent(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    risk = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value),
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule()
    view = make_view(close=98, position=make_position(), features=(plan, risk))
    assert FEATURE_APPROVED in {item.key for item in approval.on_bar(view).features}
    duplicate = approval.on_bar(view)
    assert duplicate.skips == (ReasonCode.DUPLICATE.value,)


def test_conflicting_duplicate_rejected(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    risk = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value),
        source="risk_sizing",
    )
    approval = PositionScalingApprovalModule()
    approval.on_bar(make_view(close=98, position=make_position(), features=(plan, risk)))
    changed = deep_thaw(plan.value)
    changed["metadata"]["conflict_probe"] = True
    changed_plan = feature(FEATURE_PLAN, changed, feature_id="changed-plan")
    changed_risk = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(changed),
        source="risk_sizing",
        feature_id="changed-risk",
    )
    result = approval.on_bar(
        make_view(close=98, position=make_position(), features=(changed_plan, changed_risk))
    )
    rejected = next(item.value for item in result.features if item.key == FEATURE_REJECTED)
    assert rejected["reason_code"] == ReasonCode.CONFLICT.value


def test_scale_out_plan_approved_without_risk_decision() -> None:
    parameters = {
        "enabled": True,
        "allow_scale_out": True,
        "require_setup_validity": False,
        "require_liquidation_distance": False,
        "require_margin_usage": False,
        "rules": [
            {
                "stage": 1,
                "mode": "scale_out",
                "action": "reduce",
                "trigger_kind": "r_multiple",
                "trigger_value": "1",
                "quantity_kind": "current_fraction",
                "quantity_value": "0.25",
                "expires_bars": 0,
            }
        ],
    }
    planner = PositionScalingPlannerModule(**parameters)
    planned = planner.on_bar(make_view(close=105, position=make_position(pnl=50), features=()))
    plan = next(item for item in planned.features if item.key == FEATURE_PLAN)
    approval = PositionScalingApprovalModule()
    result = approval.on_bar(make_view(close=105, position=make_position(pnl=50), features=(plan,)))
    approved = next(item.value for item in result.features if item.key == FEATURE_APPROVED)
    assert approved["approved_quantity"] == "0.25"
    assert approved["risk_decision"] is None


def test_approval_snapshot_restore_and_capacity(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = plan_feature(averaging_parameters)
    risk = feature(
        FEATURE_RISK_DECISION,
        risk_decision_for(plan.value),
        source="risk_sizing",
    )
    module = PositionScalingApprovalModule(max_processed_adjustments=1)
    module.on_bar(make_view(close=98, position=make_position(), features=(plan, risk)))
    snapshot = module.snapshot_state()
    restored = PositionScalingApprovalModule(max_processed_adjustments=1)
    restored.restore_state(snapshot)
    assert restored.snapshot_state() == snapshot
    assert restored.on_bar(
        make_view(close=98, position=make_position(), features=(plan, risk))
    ).skips == (ReasonCode.DUPLICATE.value,)


def test_approval_restore_rejects_corruption() -> None:
    module = PositionScalingApprovalModule()
    state = dict(module.snapshot_state())
    state["order"] = ["a"]
    with pytest.raises(ValueError):
        module.restore_state(state)


def test_approval_out_of_order_rejected() -> None:
    approval = PositionScalingApprovalModule()
    approval.on_bar(make_view(index=2))
    assert approval.on_bar(make_view(index=1)).skips == (ReasonCode.OUT_OF_ORDER.value,)
