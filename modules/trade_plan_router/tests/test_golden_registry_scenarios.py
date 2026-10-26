from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
import json
from pathlib import Path

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    ExistingPositionPolicy,
    LongShortConflictPolicy,
    PendingOrderState,
    PlanVerdict,
    PositionState,
    ReasonCode,
    RouterConfiguration,
    ScalingAction,
    TradeSide,
)
from modular_strategy_trade_plan_router.canonical import canonical_json

from conftest import (
    NOW,
    make_entry,
    make_package,
    make_portfolio,
    make_protection,
    make_scaling,
    make_sizing,
)


REGISTRY_PATH = Path(__file__).parent / "data" / "golden_fixtures_v1.json"
FIXTURES = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))["fixtures"]


def position(side: TradeSide, *, quantity: Decimal = Decimal("5")) -> PositionState:
    return PositionState(
        position_id=f"position-{side.value}",
        symbol="BTCUSDT",
        side=side,
        quantity=quantity,
        average_entry=Decimal("100"),
        version=f"position-{side.value}-v1",
        market="usdt_perpetual",
        strategy_id="strategy-a",
        setup_id="setup-a",
        opened_at=NOW - timedelta(hours=1),
    )


def pending(side: TradeSide) -> PendingOrderState:
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
        strategy_id="strategy-a",
        setup_id="setup-a",
        expires_at=NOW + timedelta(minutes=5),
    )


def package_for_entry(
    entry,
    *,
    portfolio=None,
    include_protection: bool = True,
    include_sizing: bool = True,
):
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    return make_package(
        entries=(entry,),
        protections=((protection,) if include_protection else ()),
        sizings=((sizing,) if include_sizing else ()),
        portfolio=portfolio,
    )


def two_candidate_package(
    *,
    first_side: TradeSide,
    second_side: TradeSide,
    first_priority: int,
    second_priority: int,
    reverse_delivery: bool = False,
):
    first = make_entry(
        candidate_id="candidate-a",
        setup_id="setup-a",
        side=first_side,
        priority=first_priority,
        created_at=NOW - timedelta(minutes=2),
    )
    second = make_entry(
        candidate_id="candidate-b",
        setup_id="setup-b",
        side=second_side,
        priority=second_priority,
        created_at=NOW - timedelta(minutes=1),
    )
    first_protection = make_protection(first)
    second_protection = make_protection(second)
    first_sizing = make_sizing(first, first_protection)
    second_sizing = make_sizing(second, second_protection)
    entries = (second, first) if reverse_delivery else (first, second)
    protections = (
        (second_protection, first_protection)
        if reverse_delivery
        else (first_protection, second_protection)
    )
    sizings = (second_sizing, first_sizing) if reverse_delivery else (first_sizing, second_sizing)
    return make_package(entries=entries, protections=protections, sizings=sizings)


def route_fixture(fixture_id: int):
    if fixture_id == 1:
        return DecisionRouter().route(make_package())
    if fixture_id == 2:
        return DecisionRouter().route(package_for_entry(make_entry(side=TradeSide.SHORT)))
    if fixture_id == 3:
        return DecisionRouter(
            RouterConfiguration(long_short_policy=LongShortConflictPolicy.HIGHER_PRIORITY)
        ).route(
            two_candidate_package(
                first_side=TradeSide.LONG,
                second_side=TradeSide.SHORT,
                first_priority=1,
                second_priority=2,
            )
        )
    if fixture_id == 4:
        return DecisionRouter().route(
            two_candidate_package(
                first_side=TradeSide.LONG,
                second_side=TradeSide.SHORT,
                first_priority=0,
                second_priority=0,
            )
        )
    if fixture_id in {5, 6}:
        side = TradeSide.LONG if fixture_id == 5 else TradeSide.SHORT
        return DecisionRouter().route(
            two_candidate_package(
                first_side=side,
                second_side=side,
                first_priority=2,
                second_priority=1,
            )
        )
    if fixture_id in {7, 14}:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(positions=(position(TradeSide.LONG),)))
        )
    if fixture_id in {8, 18}:
        return DecisionRouter().route(
            package_for_entry(
                make_entry(side=TradeSide.SHORT),
                portfolio=make_portfolio(positions=(position(TradeSide.LONG),)),
            )
        )
    if fixture_id == 9:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(positions=(position(TradeSide.SHORT),)))
        )
    if fixture_id == 10:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(pending_orders=(pending(TradeSide.LONG),)))
        )
    if fixture_id == 11:
        return DecisionRouter().route(
            package_for_entry(
                make_entry(side=TradeSide.SHORT),
                portfolio=make_portfolio(pending_orders=(pending(TradeSide.LONG),)),
            )
        )
    if fixture_id == 12:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(pending_orders=(pending(TradeSide.SHORT),)))
        )
    if fixture_id == 13:
        return DecisionRouter(
            RouterConfiguration(existing_position_policy=ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD)
        ).route(make_package(portfolio=make_portfolio(positions=(position(TradeSide.LONG),))))
    if fixture_id in {15, 16}:
        quantity = Decimal("2") if fixture_id == 15 else Decimal("5")
        target = position(TradeSide.LONG)
        entry = make_entry()
        scaling = make_scaling(
            entry,
            action=ScalingAction.REDUCE,
            quantity=quantity,
            position_id=target.position_id,
        )
        return DecisionRouter().route(
            make_package(
                entries=(),
                protections=(),
                sizings=(),
                scalings=(scaling,),
                portfolio=make_portfolio(positions=(target,)),
            )
        )
    if fixture_id == 17:
        return DecisionRouter(
            RouterConfiguration(existing_position_policy=ExistingPositionPolicy.CLOSE_THEN_REVERSE)
        ).route(make_package(portfolio=make_portfolio(positions=(position(TradeSide.SHORT),))))
    if fixture_id == 19:
        return DecisionRouter().route(package_for_entry(make_entry(), include_protection=False))
    if fixture_id == 20:
        return DecisionRouter().route(package_for_entry(make_entry(), include_sizing=False))
    if fixture_id == 21:
        entry = make_entry(
            created_at=NOW - timedelta(minutes=10),
            expires_at=NOW - timedelta(minutes=1),
        )
        return DecisionRouter().route(package_for_entry(entry))
    if fixture_id == 22:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(timestamp=NOW - timedelta(seconds=301)))
        )
    if fixture_id == 23:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(available_risk_budget=Decimal("0")))
        )
    if fixture_id == 24:
        return DecisionRouter(
            RouterConfiguration(max_symbol_notional_fraction=Decimal("0.05"))
        ).route(make_package())
    if fixture_id == 25:
        return DecisionRouter().route(
            make_package(portfolio=make_portfolio(circuit_breaker_active=True))
        )
    if fixture_id == 26:
        return DecisionRouter().route(make_package(portfolio=make_portfolio(portfolio_locked=True)))
    if fixture_id == 27:
        entry = make_entry()
        protection = make_protection(entry)
        sizing = make_sizing(entry, protection)
        return DecisionRouter().route(
            make_package(
                entries=(entry, entry),
                protections=(protection,),
                sizings=(sizing,),
            )
        )
    if fixture_id == 28:
        first = two_candidate_package(
            first_side=TradeSide.LONG,
            second_side=TradeSide.LONG,
            first_priority=2,
            second_priority=1,
        )
        second = two_candidate_package(
            first_side=TradeSide.LONG,
            second_side=TradeSide.LONG,
            first_priority=2,
            second_priority=1,
            reverse_delivery=True,
        )
        return DecisionRouter().route(first), DecisionRouter().route(second)
    if fixture_id == 29:
        package = make_package()
        router = DecisionRouter()
        return router.route(package), router.route(package)
    if fixture_id == 30:
        package = make_package()
        first = DecisionRouter().route(package)
        changed = replace(package.portfolio, version="portfolio-v2", input_hash="")
        second = DecisionRouter().route(replace(package, portfolio=changed, correlation_id=""))
        return first, second
    raise AssertionError(f"unsupported golden fixture ID: {fixture_id}")


@pytest.mark.parametrize(
    "fixture",
    FIXTURES,
    ids=lambda row: str(row["id"]),
)
def test_registered_golden_scenario(fixture: dict[str, object]) -> None:
    fixture_id = int(fixture["id"])
    result = route_fixture(fixture_id)
    invariant = fixture.get("invariant")
    if invariant == "same_result":
        first, second = result
        assert first.result_id == second.result_id
        assert canonical_json(first.to_mapping()) == canonical_json(second.to_mapping())
        assert first.verdict.value == fixture["verdict"]
        return
    if invariant == "different_plan_id_and_occ":
        first, second = result
        assert first.plan is not None and second.plan is not None
        assert first.plan.plan_id != second.plan.plan_id
        first_occ = {row.kind: row.expected for row in first.plan.preconditions}
        second_occ = {row.kind: row.expected for row in second.plan.preconditions}
        assert first_occ["portfolio_version"] != second_occ["portfolio_version"]
        assert first.verdict.value == fixture["verdict"]
        return

    assert result.verdict.value == fixture["verdict"]
    if "action" in fixture:
        assert result.plan is not None
        assert result.plan.action.value == fixture["action"]
    if "reason" in fixture:
        expected = ReasonCode(str(fixture["reason"]))
        assert expected in result.reason_codes
    if result.verdict is PlanVerdict.ACCEPTED:
        assert result.plan is not None
        assert result.reason_codes == (ReasonCode.ACCEPTED,)
    else:
        assert result.plan is None
