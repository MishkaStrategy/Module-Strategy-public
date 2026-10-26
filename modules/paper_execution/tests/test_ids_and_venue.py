from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from modular_core import OrderType, Side, TimeInForce

from paper_execution import (
    ClientOrderIdFactory,
    ExecutionIntent,
    IntentRole,
    PaperReasonCode,
    VenueRuleEngine,
    VenueRuleRejection,
)


def test_client_order_id_is_deterministic_and_bounded() -> None:
    factory = ClientOrderIdFactory("pevr", 36)
    first = factory.create(plan_id="plan", intent_id="entry", role="entry")
    second = factory.create(plan_id="plan", intent_id="entry", role="entry")
    assert first == second
    assert len(first) <= 36
    assert set(first) <= set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-")


def test_client_order_id_changes_with_revision() -> None:
    factory = ClientOrderIdFactory()
    assert factory.create(plan_id="p", intent_id="i", role="entry", revision=0) != factory.create(
        plan_id="p", intent_id="i", role="entry", revision=1
    )


def test_client_order_id_collision_resistance_sample() -> None:
    factory = ClientOrderIdFactory()
    values = {
        factory.create(plan_id=f"p-{index}", intent_id=f"i-{index % 17}", role="entry")
        for index in range(20_000)
    }
    assert len(values) == 20_000


def test_tick_step_and_notional_normalization(entry_intent, metadata, now) -> None:
    result = VenueRuleEngine().normalize(
        intent=entry_intent,
        metadata=metadata,
        client_order_id="pevr-entry-0-1234567890123456",
        reference_price=Decimal("100000"),
        event_time=now,
    )
    assert result.audit.normalized_quantity == Decimal("0.012")
    assert result.audit.normalized_price == Decimal("99999.9")
    assert result.audit.notional == Decimal("1199.9988")
    assert result.request.quantity == pytest.approx(0.012)


def test_unsupported_order_type_is_typed_rejection(entry_intent, metadata, now) -> None:
    restricted = replace(metadata, permitted_order_types=(OrderType.MARKET,))
    with pytest.raises(VenueRuleRejection) as exc:
        VenueRuleEngine().normalize(
            intent=entry_intent,
            metadata=restricted,
            client_order_id="safe-id-1234567890123456",
            reference_price=Decimal("100000"),
            event_time=now,
        )
    assert exc.value.reason_code is PaperReasonCode.UNSUPPORTED_PAPER_ORDER_TYPE


def test_min_notional_is_rejected(entry_intent, metadata, now) -> None:
    tiny = replace(entry_intent, quantity=Decimal("0.001"), limit_price=Decimal("100"))
    with pytest.raises(VenueRuleRejection) as exc:
        VenueRuleEngine().normalize(
            intent=tiny,
            metadata=metadata,
            client_order_id="safe-id-1234567890123456",
            reference_price=Decimal("100"),
            event_time=now,
        )
    assert exc.value.reason_code is PaperReasonCode.VENUE_RULE_REJECTED


def test_post_only_capability_is_enforced(entry_intent, metadata, now) -> None:
    intent = replace(entry_intent, post_only=True)
    with pytest.raises(VenueRuleRejection):
        VenueRuleEngine().normalize(
            intent=intent,
            metadata=replace(metadata, post_only_supported=False),
            client_order_id="safe-id-1234567890123456",
            reference_price=Decimal("100000"),
            event_time=now,
        )


def test_stop_on_unsafe_side_is_rejected(metadata, now) -> None:
    intent = ExecutionIntent(
        intent_id="stop",
        role=IntentRole.STOP,
        side=Side.SELL,
        order_type=OrderType.STOP_MARKET,
        quantity=Decimal("0.01"),
        trigger_price=Decimal("101000"),
        reduce_only=True,
    )
    with pytest.raises(VenueRuleRejection, match="unsafe side"):
        VenueRuleEngine().normalize(
            intent=intent,
            metadata=metadata,
            client_order_id="safe-id-1234567890123456",
            reference_price=Decimal("100000"),
            event_time=now,
        )


def test_disallowed_time_in_force_is_rejected(entry_intent, metadata, now) -> None:
    intent = replace(entry_intent, time_in_force=TimeInForce.IOC)
    with pytest.raises(VenueRuleRejection):
        VenueRuleEngine().normalize(
            intent=intent,
            metadata=replace(metadata, permitted_time_in_force=(TimeInForce.GTC,)),
            client_order_id="safe-id-1234567890123456",
            reference_price=Decimal("100000"),
            event_time=now,
        )
