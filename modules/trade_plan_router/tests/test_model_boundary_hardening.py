from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import models
from modular_strategy_trade_plan_router.enums import (
    ScalingAction,
    ScalingDirection,
    TradeSide,
)
from modular_strategy_trade_plan_router.errors import RouterInputError
from modular_strategy_trade_plan_router.models import (
    PendingOrderState,
    PositionState,
    TargetIntent,
)

from conftest import (
    NOW,
    make_entry,
    make_portfolio,
    make_protection,
    make_scaling,
    make_sizing,
)


def position(
    *,
    position_id: str = "position-a",
    opened_at=NOW - timedelta(hours=1),
) -> PositionState:
    return PositionState(
        position_id=position_id,
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=Decimal("10"),
        average_entry=Decimal("100"),
        version=f"{position_id}-v1",
        market="usdt_perpetual",
        strategy_id="strategy-a",
        setup_id="setup-a",
        initial_risk=Decimal("50"),
        remaining_risk=Decimal("40"),
        reserved_risk=Decimal("0"),
        opened_at=opened_at,
        scaling_stage=1,
    )


def order(
    *,
    order_id: str = "order-a",
    created_at=NOW - timedelta(minutes=1),
) -> PendingOrderState:
    return PendingOrderState(
        order_intent_id=order_id,
        plan_id="plan-a",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        order_type="limit",
        quantity=Decimal("1"),
        status="pending",
        version=f"{order_id}-v1",
        created_at=created_at,
        price=Decimal("100"),
        reduce_only=False,
        post_only=False,
        reserved_risk=Decimal("1"),
        strategy_id="strategy-a",
        setup_id="setup-a",
        expires_at=NOW + timedelta(minutes=5),
    )


def test_model_primitive_text_digest_boolean_integer_and_time_boundaries() -> None:
    for value in (None, "", "line\nbreak"):
        with pytest.raises(RouterInputError):
            models._text(value, "field")  # type: ignore[arg-type]
    with pytest.raises(RouterInputError, match="maximum length"):
        models._text("long", "field", max_length=3)
    assert models._optional_text(None, "field") is None
    with pytest.raises(RouterInputError, match="SHA-256"):
        models._sha256_text("bad", "digest")
    assert models._sha256_text("A" * 64, "digest") == "a" * 64
    with pytest.raises(RouterInputError, match="boolean"):
        models._boolean(1, "flag")
    for value in (True, 1.5, "1"):
        with pytest.raises(RouterInputError, match="integer"):
            models._bounded_int(value, "count", minimum=0)
    with pytest.raises(RouterInputError, match="range"):
        models._bounded_int(-1, "count", minimum=0)
    with pytest.raises(RouterInputError, match="timezone-aware"):
        models._utc(datetime(2026, 1, 1), "time")


def test_model_decimal_mapping_and_sequence_boundaries(monkeypatch) -> None:
    for value in (True, object(), "bad", "NaN", "-1"):
        with pytest.raises(RouterInputError):
            models._decimal(value, "amount")  # type: ignore[arg-type]
    with pytest.raises(RouterInputError, match="positive"):
        models._decimal("0", "amount", positive=True)
    assert models._optional_decimal(None, "amount") is None
    assert dict(models._frozen_mapping(None)) == {}
    with pytest.raises(RouterInputError, match="must be a mapping"):
        models._frozen_mapping([])  # type: ignore[arg-type]
    with pytest.raises(RouterInputError, match="invalid mapping content"):
        models._frozen_mapping({"bad": object()})
    with pytest.raises(RouterInputError, match="built-in list or tuple"):
        models._plain_sequence({"a"}, "values")
    monkeypatch.setattr(models, "MAX_ROUTER_COLLECTION_ITEMS", 1)
    with pytest.raises(RouterInputError, match="maximum item count"):
        models._plain_sequence([1, 2], "values")


def test_entry_candidate_time_schema_type_and_numeric_invariants() -> None:
    entry = make_entry()
    invalid = (
        {"schema_version": "2"},
        {"side": "long"},
        {"post_only": 1},
        {"available_at": entry.signal_time - timedelta(seconds=1)},
        {"created_at": entry.available_at - timedelta(seconds=1)},
        {"expires_at": entry.created_at},
        {"ttl_bars": 0},
        {"priority": models.MAX_PRIORITY_ABS + 1},
        {"confidence": Decimal("1.1")},
        {"incompatibility_tags": {"tag"}},
        {"input_hash": "0" * 64},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(entry, **values)  # type: ignore[arg-type]


def test_entry_candidate_order_geometry_matrix() -> None:
    entry = make_entry()
    invalid = (
        {
            "order_type": "market",
            "price": Decimal("100"),
            "limit_price": None,
            "trigger_price": None,
        },
        {
            "order_type": "market",
            "price": None,
            "limit_price": None,
            "trigger_price": None,
            "post_only": True,
        },
        {"order_type": "limit", "price": None, "limit_price": None},
        {
            "order_type": "stop_market",
            "price": None,
            "limit_price": None,
            "trigger_price": None,
        },
        {
            "order_type": "stop_limit",
            "price": None,
            "limit_price": None,
            "trigger_price": Decimal("100"),
        },
        {"order_type": "unsupported"},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(entry, **values)


def test_target_and_protection_decision_invariants() -> None:
    with pytest.raises(RouterInputError, match="allocation"):
        TargetIntent(Decimal("110"), Decimal("1.1"))
    entry = make_entry()
    protection = make_protection(entry)
    invalid = (
        {"schema_version": "2"},
        {"side": "long"},
        {"expires_at": protection.created_at},
        {"targets": ()},
        {"targets": (object(),)},
        {
            "targets": (
                TargetIntent(Decimal("110"), Decimal("0.6")),
                TargetIntent(Decimal("120"), Decimal("0.6")),
            )
        },
        {"max_holding_seconds": 0},
        {"risk_distance": Decimal("999")},
        {"input_hash": "0" * 64},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(protection, **values)  # type: ignore[arg-type]


def test_sizing_decision_invariants() -> None:
    entry = make_entry()
    protection = make_protection(entry)
    approved = make_sizing(entry, protection)
    invalid = (
        {"schema_version": "2"},
        {"approved": 1},
        {"quantity": Decimal("0")},
        {"notional": Decimal("0")},
        {"risk_amount": Decimal("0")},
        {"risk_fraction": Decimal("1.1")},
        {"expires_at": approved.created_at},
        {"binding_constraints": {"constraint"}},
        {"input_hash": "0" * 64},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(approved, **values)  # type: ignore[arg-type]

    rejected = make_sizing(
        entry,
        protection,
        approved=False,
        quantity=Decimal("0"),
        risk_amount=Decimal("0"),
        risk_fraction=Decimal("0"),
        notional=Decimal("0"),
    )
    with pytest.raises(RouterInputError, match="quantity must be zero"):
        replace(rejected, quantity=Decimal("1"))


def test_scaling_decision_schema_action_direction_and_risk_invariants() -> None:
    entry = make_entry()
    scaling = make_scaling(entry)
    invalid = (
        {"schema_version": "2"},
        {"side": "long"},
        {"action": "add"},
        {"order_type": "unsupported"},
        {"order_type": "stop_market", "trigger_price": None},
        {"action": ScalingAction.REBALANCE, "direction": None},
        {"direction": "increase"},
        {"action": ScalingAction.ADD, "direction": ScalingDirection.DECREASE},
        {
            "action": ScalingAction.REBALANCE,
            "direction": ScalingDirection.NONE,
        },
        {"risk_request_id": None},
        {"approved_quantity": Decimal("0")},
        {"stage": 0},
        {"expires_at": scaling.created_at},
        {"input_hash": "0" * 64},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(scaling, **values)  # type: ignore[arg-type]

    reduction = make_scaling(entry, action=ScalingAction.REDUCE)
    with pytest.raises(RouterInputError, match="cannot reserve risk"):
        replace(reduction, risk_request_id="risk-a")
    hold = replace(
        reduction,
        action=ScalingAction.HOLD,
        direction=ScalingDirection.NONE,
        approved_quantity=Decimal("0"),
        input_hash="",
    )
    with pytest.raises(RouterInputError, match="zero quantity"):
        replace(hold, approved_quantity=Decimal("1"))


def test_position_state_invariants() -> None:
    value = position()
    invalid = (
        {"side": "long"},
        {"quantity": Decimal("0")},
        {"average_entry": Decimal("0")},
        {"remaining_risk": Decimal("-1")},
        {"opened_at": datetime(2026, 1, 1)},
        {"scaling_stage": -1},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(value, **values)  # type: ignore[arg-type]


def test_pending_order_type_status_geometry_boolean_and_time_invariants() -> None:
    value = order()
    invalid = (
        {"side": "long"},
        {"order_type": "unsupported"},
        {"status": "unsupported"},
        {"price": None},
        {"order_type": "stop_market", "price": None, "stop_price": None},
        {"order_type": "market", "price": None, "post_only": True},
        {"reduce_only": 1},
        {"expires_at": value.created_at},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(value, **values)  # type: ignore[arg-type]


def test_portfolio_snapshot_collection_temporal_identity_and_hash_invariants() -> None:
    valid_position = position()
    valid_order = order()
    portfolio = make_portfolio(
        positions=(valid_position,),
        pending_orders=(valid_order,),
    )
    invalid = (
        {"schema_version": "2"},
        {"portfolio_locked": 1},
        {"positions": (object(),)},
        {"pending_orders": (object(),)},
        {"positions": (position(opened_at=NOW + timedelta(seconds=1)),)},
        {"pending_orders": (order(created_at=NOW + timedelta(seconds=1)),)},
        {"positions": (valid_position, valid_position)},
        {"pending_orders": (valid_order, valid_order)},
        {"input_hash": "0" * 64},
    )
    for values in invalid:
        with pytest.raises(RouterInputError):
            replace(portfolio, **values)  # type: ignore[arg-type]
