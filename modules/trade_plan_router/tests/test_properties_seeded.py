from __future__ import annotations

import random
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    LongShortConflictPolicy,
    PendingOrderPolicy,
    PlanVerdict,
    ReasonCode,
    RouterConfiguration,
    TradeSide,
)
from modular_strategy_trade_plan_router.errors import RouterInputError
from modular_strategy_trade_plan_router.models import PendingOrderState
from modular_strategy_trade_plan_router.normalizer import DecisionNormalizer

from conftest import NOW, make_entry, make_package, make_portfolio, make_protection, make_sizing


def _candidate_set(count: int):
    entries = []
    protections = []
    sizings = []
    for index in range(count):
        entry = make_entry(
            candidate_id=f"entry-{index:04d}",
            setup_id=f"setup-{index:04d}",
            priority=count - index,
        )
        protection = make_protection(entry)
        sizing = make_sizing(entry, protection)
        entries.append(entry)
        protections.append(protection)
        sizings.append(sizing)
    return tuple(entries), tuple(protections), tuple(sizings)


def test_seeded_permutations_are_routing_stable():
    entries, protections, sizings = _candidate_set(20)
    router = DecisionRouter()
    baseline = router.route(make_package(entries=entries, protections=protections, sizings=sizings))
    assert baseline.plan is not None
    rng = random.Random(20260723)
    for _ in range(50):
        entry_rows = list(entries)
        protection_rows = list(protections)
        sizing_rows = list(sizings)
        rng.shuffle(entry_rows)
        rng.shuffle(protection_rows)
        rng.shuffle(sizing_rows)
        replay = router.route(
            make_package(
                entries=tuple(entry_rows),
                protections=tuple(protection_rows),
                sizings=tuple(sizing_rows),
            )
        )
        assert replay.to_mapping() == baseline.to_mapping()


def test_adding_strictly_lower_priority_candidate_is_metamorphically_neutral():
    winner = make_entry(candidate_id="winner", setup_id="winner", priority=100)
    winner_protection = make_protection(winner)
    winner_sizing = make_sizing(winner, winner_protection)
    baseline = DecisionRouter().route(
        make_package(
            entries=(winner,),
            protections=(winner_protection,),
            sizings=(winner_sizing,),
        )
    )
    loser = make_entry(candidate_id="loser", setup_id="loser", priority=-100)
    loser_protection = make_protection(loser)
    loser_sizing = make_sizing(loser, loser_protection)
    extended = DecisionRouter().route(
        make_package(
            entries=(loser, winner),
            protections=(loser_protection, winner_protection),
            sizings=(loser_sizing, winner_sizing),
        )
    )
    assert baseline.plan is not None and extended.plan is not None
    assert baseline.plan.selected_candidate_id == extended.plan.selected_candidate_id == "winner"
    # Audit/rejected-candidate evidence changes, but trading semantics do not.
    assert baseline.plan.entry_intent == extended.plan.entry_intent
    assert baseline.plan.protection_intent == extended.plan.protection_intent
    assert baseline.plan.sizing_intent == extended.plan.sizing_intent


def test_increasing_caps_cannot_reject_previously_accepted_plan():
    package = make_package()
    tight = DecisionRouter(
        RouterConfiguration(
            max_portfolio_risk_fraction=Decimal("0.005"),
            max_symbol_notional_fraction=Decimal("0.10"),
        )
    ).route(package)
    loose = DecisionRouter(
        RouterConfiguration(
            max_portfolio_risk_fraction=Decimal("0.05"),
            max_symbol_notional_fraction=Decimal("1"),
        )
    ).route(package)
    assert tight.verdict is PlanVerdict.ACCEPTED
    assert loose.verdict is PlanVerdict.ACCEPTED


def test_removing_conflicting_pending_order_unblocks_plan():
    pending = PendingOrderState(
        order_intent_id="pending-short",
        plan_id="old",
        symbol="BTCUSDT",
        side=TradeSide.SHORT,
        order_type="limit",
        quantity=Decimal("1"),
        status="pending",
        version="o1",
        created_at=NOW,
        price=Decimal("100"),
    )
    blocked = DecisionRouter().route(
        make_package(portfolio=make_portfolio(pending_orders=(pending,)))
    )
    unblocked = DecisionRouter().route(make_package(portfolio=make_portfolio()))
    assert blocked.reason_codes == (ReasonCode.PENDING_ORDER_CONFLICT,)
    assert unblocked.verdict is PlanVerdict.ACCEPTED


def test_cancel_opposite_policy_is_deterministic_across_pending_order_permutations():
    orders = tuple(
        PendingOrderState(
            order_intent_id=f"order-{index}",
            plan_id=f"plan-{index}",
            symbol="BTCUSDT",
            side=TradeSide.SHORT,
            order_type="limit",
            quantity=Decimal("1"),
            status="pending",
            version=f"v{index}",
            created_at=NOW,
            price=Decimal("100"),
        )
        for index in range(10)
    )
    router = DecisionRouter(
        RouterConfiguration(pending_order_policy=PendingOrderPolicy.CANCEL_OPPOSITE)
    )
    first = router.route(make_package(portfolio=make_portfolio(pending_orders=orders)))
    second = router.route(
        make_package(portfolio=make_portfolio(pending_orders=tuple(reversed(orders))))
    )
    assert first.to_mapping() == second.to_mapping()
    assert first.plan is not None
    assert [item.order_intent_id for item in first.plan.cancellation_intents] == sorted(
        item.order_intent_id for item in orders
    )


def test_concurrent_duplicate_delivery_is_stateless_and_byte_stable():
    router = DecisionRouter()
    package = make_package()

    def run_once(_: int):
        return router.route(package).to_mapping()

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(run_once, range(256)))
    assert all(result == results[0] for result in results)


def test_long_short_priority_is_stable_under_many_seeded_permutations():
    entries = []
    protections = []
    sizings = []
    for index in range(30):
        side = TradeSide.LONG if index % 2 == 0 else TradeSide.SHORT
        entry = make_entry(
            candidate_id=f"candidate-{index:02d}",
            setup_id=f"setup-{index:02d}",
            side=side,
            priority=index,
        )
        protection = make_protection(entry)
        sizing = make_sizing(entry, protection)
        entries.append(entry)
        protections.append(protection)
        sizings.append(sizing)
    router = DecisionRouter(
        RouterConfiguration(long_short_policy=LongShortConflictPolicy.HIGHER_PRIORITY)
    )
    baseline = router.route(
        make_package(entries=tuple(entries), protections=tuple(protections), sizings=tuple(sizings))
    )
    assert baseline.plan is not None
    assert baseline.plan.selected_candidate_id == "candidate-29"
    rng = random.Random(17)
    for _ in range(30):
        rng.shuffle(entries)
        rng.shuffle(protections)
        rng.shuffle(sizings)
        result = router.route(
            make_package(
                entries=tuple(entries), protections=tuple(protections), sizings=tuple(sizings)
            )
        )
        assert result.to_mapping() == baseline.to_mapping()


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        "text",
        {"schema_version": "2"},
        {"schema_version": "1", "portfolio_id": "x", "version": "v", "positions": object()},
        {"schema_version": "1", "portfolio_id": "x", "version": "v", "pending_orders": object()},
    ],
)
def test_malformed_portfolio_payloads_fail_closed(payload):
    normalizer = DecisionNormalizer()
    if not isinstance(payload, dict):
        with pytest.raises(RouterInputError):
            normalizer.normalize_portfolio(payload, feature_created_at=NOW)  # type: ignore[arg-type]
    else:
        with pytest.raises(RouterInputError):
            normalizer.normalize_portfolio(payload, feature_created_at=NOW)


def test_seeded_mutation_fuzz_never_creates_plan_from_invalid_entry_identity():
    from test_normalizer_contracts import actual_entry_payload

    rng = random.Random(90125)
    normalizer = DecisionNormalizer(strict_mode=True)
    keys = ["symbol", "side", "ttl_bars", "post_only", "setup_fingerprint", "created_at"]
    for _ in range(250):
        payload = actual_entry_payload()
        key = rng.choice(keys)
        payload[key] = rng.choice([None, True, -1, "", "bad", [], {}, "ETHUSDT"])
        with pytest.raises(RouterInputError):
            normalizer.normalize_entry(
                payload,
                feature_key="entry_model.plan",
                feature_schema_version="2",
                feature_created_at=NOW,
                feature_valid_until=None,
                feature_id="fuzz-entry",
                source_module="entry_model",
                symbol="BTCUSDT",
            )
