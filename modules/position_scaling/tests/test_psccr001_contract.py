from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
from conftest import feature, make_position, make_view, standard_features

from modular_strategy_position_scaling import (
    FEATURE_PLAN,
    FEATURE_RISK_DECISION,
    PositionScalingApprovalModule,
    PositionScalingPlannerModule,
)
from modular_strategy_position_scaling.decimal_utils import weighted_average
from modular_strategy_position_scaling.enums import ReasonCode


def _plan(parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    planner = PositionScalingPlannerModule(**parameters)
    decision = planner.on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=standard_features(),
        )
    )
    return next(item.value for item in decision.features if item.key == FEATURE_PLAN)


def _risk_decision(
    plan: Mapping[str, Any],
    *,
    approved_quantity: str = "0.1",
    adjustment_id: str | None = None,
    projected_total_quantity: str | None = None,
) -> Mapping[str, Any]:
    quantity = Decimal(approved_quantity)
    current_quantity = Decimal(str(plan["current_quantity"]))
    current_average = Decimal(str(plan["current_average_price"]))
    desired_price = Decimal(str(plan["desired_price"]))
    expected_total = current_quantity + quantity
    expected_average = weighted_average(
        current_average,
        current_quantity,
        desired_price,
        quantity,
    )
    return {
        "schema_version": "1",
        "request_id": plan["risk_request_id"],
        "symbol": plan["symbol"],
        "status": "approved",
        "approved": True,
        "quantity": approved_quantity,
        "raw_quantity": approved_quantity,
        "risk_fraction": "0.001",
        "risk_budget": "10",
        "actual_risk_amount": "1",
        "actual_risk_fraction": "0.0001",
        "notional": "9.8",
        "required_leverage": "0.001",
        "binding_constraints": [],
        "reject_reason": None,
        "message": "",
        "setup_id": plan["setup_id"],
        "diagnostics": {},
        "position_adjustment": {
            "schema_version": "1",
            "adjustment_id": adjustment_id or plan["adjustment_id"],
            "position_id": plan["position_id"],
            "requested_quantity": plan["desired_quantity"],
            "approved_quantity": approved_quantity,
            "projected_total_quantity": (
                str(expected_total)
                if projected_total_quantity is None
                else projected_total_quantity
            ),
            "projected_average_price": str(expected_average),
            "projected_notional": "107.8",
            "projected_leverage": "0.01078",
            "projected_total_risk": "5.3",
            "opens_new_position": False,
        },
    }


def _mutated_typed_decision(
    plan: Mapping[str, Any],
    field: str,
    value: object,
) -> Mapping[str, Any]:
    decision = dict(_risk_decision(plan))
    adjustment = dict(decision["position_adjustment"])
    adjustment[field] = value
    decision["position_adjustment"] = adjustment
    return decision


def _approve(
    parameters: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> Mapping[str, Any]:
    planner = PositionScalingPlannerModule(**parameters)
    planned = planner.on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=standard_features(),
        )
    )
    plan_feature = next(item for item in planned.features if item.key == FEATURE_PLAN)
    risk_feature = feature(
        FEATURE_RISK_DECISION,
        risk,
        source="risk_sizing",
    )
    result = PositionScalingApprovalModule().on_bar(
        make_view(
            close=98,
            position=make_position(),
            features=(plan_feature, risk_feature),
        )
    )
    return next(item.value for item in result.features)


def _assert_invalid(result: Mapping[str, Any]) -> None:
    assert result["reason_code"] == ReasonCode.RISK_DECISION_INVALID.value


def test_typed_risk_adjustment_is_accepted(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    result = _approve(
        averaging_parameters,
        _risk_decision(plan),
    )
    assert result["schema_version"] == "1"
    assert result["approved_quantity"] == "0.1"


def test_typed_risk_adjustment_identity_mismatch_is_rejected(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    result = _approve(
        averaging_parameters,
        _risk_decision(plan, adjustment_id="other-adjustment"),
    )
    _assert_invalid(result)


def test_typed_risk_adjustment_geometry_mismatch_is_rejected(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    result = _approve(
        averaging_parameters,
        _risk_decision(plan, projected_total_quantity="999"),
    )
    _assert_invalid(result)


def test_legacy_risk_decision_remains_backward_compatible(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    legacy = dict(_risk_decision(plan))
    legacy.pop("position_adjustment")
    result = _approve(averaging_parameters, legacy)
    assert result["approved_quantity"] == "0.1"
    assert result["risk_decision"].get("position_adjustment") is None


def test_typed_decision_never_opens_a_new_position(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    result = _approve(
        averaging_parameters,
        _mutated_typed_decision(plan, "opens_new_position", True),
    )
    _assert_invalid(result)


def test_typed_decision_payload_must_be_a_mapping(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    decision = dict(_risk_decision(plan))
    decision["position_adjustment"] = []
    _assert_invalid(_approve(averaging_parameters, decision))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unknown_field", "unexpected"),
        ("schema_version", "2"),
        ("position_id", "other-position"),
        ("requested_quantity", "999"),
        ("approved_quantity", "0.05"),
        ("projected_average_price", "999"),
        ("projected_notional", "-1"),
        ("projected_leverage", "-1"),
        ("projected_total_risk", "-1"),
    ],
)
def test_typed_decision_invalid_fields_fail_closed(
    averaging_parameters: Mapping[str, Any],
    field: str,
    value: object,
) -> None:
    plan = _plan(averaging_parameters)
    result = _approve(
        averaging_parameters,
        _mutated_typed_decision(plan, field, value),
    )
    _assert_invalid(result)


def test_typed_decision_cannot_approve_above_plan(
    averaging_parameters: Mapping[str, Any],
) -> None:
    plan = _plan(averaging_parameters)
    result = _approve(
        averaging_parameters,
        _risk_decision(plan, approved_quantity="10"),
    )
    _assert_invalid(result)
