from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    ExistingPositionPolicy,
    PlanVerdict,
    PositionState,
    ReasonCode,
    RouterConfiguration,
    TradeAction,
    TradeSide,
)
from modular_strategy_trade_plan_router.models import PendingOrderState

from conftest import NOW, make_entry, make_package, make_portfolio, make_protection, make_sizing


def _position(side: TradeSide) -> PositionState:
    return PositionState(
        position_id=f"position-{side.value}",
        symbol="BTCUSDT",
        side=side,
        quantity=Decimal("5"),
        average_entry=Decimal("100"),
        version=f"position-{side.value}-v1",
        setup_id="setup-a",
        opened_at=NOW - timedelta(hours=1),
    )


def _pending(side: TradeSide) -> PendingOrderState:
    return PendingOrderState(
        order_intent_id=f"pending-{side.value}",
        plan_id="old-plan",
        symbol="BTCUSDT",
        side=side,
        order_type="limit",
        quantity=Decimal("1"),
        status="pending",
        version=f"pending-{side.value}-v1",
        created_at=NOW - timedelta(minutes=1),
        price=Decimal("100"),
        setup_id="setup-a",
        expires_at=NOW + timedelta(minutes=5),
    )


def test_golden_two_short_setups_choose_explicit_higher_priority():
    first = make_entry(candidate_id="short-a", setup_id="short-a", side=TradeSide.SHORT, priority=7)
    second = make_entry(
        candidate_id="short-b", setup_id="short-b", side=TradeSide.SHORT, priority=2
    )
    protections = (make_protection(first), make_protection(second))
    sizings = (make_sizing(first, protections[0]), make_sizing(second, protections[1]))
    result = DecisionRouter().route(
        make_package(entries=(second, first), protections=protections, sizings=sizings)
    )
    assert result.plan is not None
    assert result.plan.selected_candidate_id == first.candidate_id
    assert result.plan.side is TradeSide.SHORT


@pytest.mark.parametrize(
    ("position_side", "entry_side"),
    ((TradeSide.LONG, TradeSide.SHORT), (TradeSide.SHORT, TradeSide.LONG)),
)
def test_golden_opposite_position_rejected_without_reversal_policy(position_side, entry_side):
    entry = make_entry(side=entry_side)
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    result = DecisionRouter().route(
        make_package(
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
            portfolio=make_portfolio(positions=(_position(position_side),)),
        )
    )
    assert result.verdict is PlanVerdict.REJECTED
    assert result.reason_codes == (ReasonCode.POSITION_CONFLICT,)


@pytest.mark.parametrize(
    ("pending_side", "entry_side"),
    ((TradeSide.LONG, TradeSide.SHORT), (TradeSide.SHORT, TradeSide.LONG)),
)
def test_golden_opposite_pending_order_rejected_by_default(pending_side, entry_side):
    entry = make_entry(side=entry_side)
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    result = DecisionRouter().route(
        make_package(
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
            portfolio=make_portfolio(pending_orders=(_pending(pending_side),)),
        )
    )
    assert result.reason_codes == (ReasonCode.PENDING_ORDER_CONFLICT,)


def test_golden_snapshot_version_change_changes_plan_identity_and_precondition():
    package = make_package()
    first = DecisionRouter().route(package)
    changed_portfolio = replace(package.portfolio, version="portfolio-v2", input_hash="")
    second = DecisionRouter().route(
        replace(package, portfolio=changed_portfolio, correlation_id="")
    )

    assert first.plan is not None and second.plan is not None
    assert first.plan.plan_id != second.plan.plan_id
    first_preconditions = {item.kind: item.expected for item in first.plan.preconditions}
    second_preconditions = {item.kind: item.expected for item in second.plan.preconditions}
    assert first_preconditions["portfolio_version"] == "portfolio-v1"
    assert second_preconditions["portfolio_version"] == "portfolio-v2"


def test_golden_allowed_reversal_has_close_before_open_action():
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.CLOSE_THEN_REVERSE)
    ).route(make_package(portfolio=make_portfolio(positions=(_position(TradeSide.SHORT),))))
    assert result.plan is not None
    assert result.plan.action is TradeAction.REVERSE_POSITION
    assert result.plan.sequencing_rules[0] == "close_target_position_before_open"
