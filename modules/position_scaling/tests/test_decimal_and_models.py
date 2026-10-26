from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from modular_strategy_position_scaling.canonical import canonical_json, fingerprint, stable_id
from modular_strategy_position_scaling.decimal_utils import (
    canonical_decimal,
    decimal_from,
    weighted_average,
)
from modular_strategy_position_scaling.enums import (
    AdjustmentAction,
    AdjustmentDirection,
    ReasonCode,
    ScalingMode,
)
from modular_strategy_position_scaling.models import PositionAdjustmentPlan


def make_plan(**overrides: object) -> PositionAdjustmentPlan:
    values: dict[str, object] = {
        "adjustment_id": "adjustment-1",
        "position_id": "position-1",
        "setup_id": "setup-1",
        "symbol": "BTCUSDT",
        "side": "long",
        "action": AdjustmentAction.ADD,
        "direction": AdjustmentDirection.INCREASE,
        "scaling_mode": ScalingMode.AVERAGING_DOWN,
        "stage": 1,
        "decision_time": datetime(2026, 1, 1, tzinfo=UTC),
        "decision_index": 1,
        "desired_entry_kind": "limit",
        "desired_price": Decimal("99"),
        "trigger_price": Decimal("99"),
        "desired_quantity": Decimal("0.25"),
        "maximum_quantity": Decimal("0.25"),
        "risk_budget": Decimal("25"),
        "notional_budget": Decimal("30000"),
        "current_average_price": Decimal("100"),
        "expected_average_price": Decimal("99.8"),
        "current_quantity": Decimal("1"),
        "expected_total_quantity": Decimal("1.25"),
        "current_total_risk": Decimal("5"),
        "projected_total_risk": Decimal("6"),
        "maximum_total_risk": Decimal("50"),
        "stop_price": Decimal("95"),
        "invalidation_price": Decimal("95"),
        "expires_at": datetime(2026, 1, 1, tzinfo=UTC),
        "expires_index": 1,
        "reason_code": ReasonCode.RISK_PENDING,
        "risk_request_id": "riskreq-1",
        "metadata": {"nested": {"a": [1, 2]}},
        "source_feature_ids": ("feat-1",),
        "config_fingerprint": "f" * 64,
    }
    values.update(overrides)
    return PositionAdjustmentPlan(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [float("nan"), float("inf"), "NaN", "Infinity"])
def test_decimal_rejects_non_finite(value: object) -> None:
    with pytest.raises(ValueError):
        decimal_from(value, "value")


def test_decimal_rejects_bool() -> None:
    with pytest.raises(TypeError):
        decimal_from(True, "value")


def test_canonical_decimal_is_stable() -> None:
    assert canonical_decimal(Decimal("1.2300")) == "1.23"
    assert canonical_decimal(Decimal("-0.000")) == "0"


def test_weighted_average_exact_decimal() -> None:
    result = weighted_average(Decimal("100"), Decimal("1"), Decimal("90"), Decimal("0.5"))
    assert result == Decimal("96.66666666666666666666666666666667")


def test_weighted_average_rejects_zero_quantity() -> None:
    with pytest.raises(ValueError):
        weighted_average(Decimal("100"), Decimal("0"), Decimal("90"), Decimal("1"))


def test_canonical_json_handles_datetime_and_decimal() -> None:
    payload = {"d": Decimal("1.00"), "t": datetime(2026, 1, 1, tzinfo=UTC)}
    assert canonical_json(payload) == '{"d":"1","t":"2026-01-01T00:00:00Z"}'


def test_fingerprint_and_stable_id_are_deterministic() -> None:
    payload = {"b": 2, "a": Decimal("1.0")}
    assert fingerprint(payload) == fingerprint({"a": Decimal("1"), "b": 2})
    assert stable_id("x", payload) == stable_id("x", payload)


def test_plan_roundtrip_and_deep_immutability() -> None:
    plan = make_plan()
    restored = PositionAdjustmentPlan.from_mapping(plan.to_mapping())
    assert restored == plan
    with pytest.raises(TypeError):
        restored.metadata["new"] = 1  # type: ignore[index]


def test_plan_rejects_unknown_field() -> None:
    payload = dict(make_plan().to_mapping())
    payload["future"] = 1
    with pytest.raises(ValueError, match="unknown plan fields"):
        PositionAdjustmentPlan.from_mapping(payload)


def test_plan_rejects_zero_increase_quantity() -> None:
    with pytest.raises(ValueError):
        make_plan(desired_quantity=Decimal("0"), maximum_quantity=Decimal("0"))


def test_plan_allows_zero_quantity_for_stop_scaling() -> None:
    plan = make_plan(
        action=AdjustmentAction.STOP_SCALING,
        direction=AdjustmentDirection.NONE,
        desired_quantity=Decimal("0"),
        maximum_quantity=Decimal("0"),
        expected_total_quantity=Decimal("1"),
        expected_average_price=Decimal("100"),
        risk_request_id=None,
        reason_code=ReasonCode.STOP_SCALING_REQUESTED,
    )
    assert plan.direction is AdjustmentDirection.NONE
