from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from modular_core import (
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

from paper_execution.enums import PaperOrderState
from paper_execution.exceptions import OrderStateError
from paper_execution.state_machine import OrderAggregate


def _snapshot(
    *,
    status: OrderStatus,
    filled: float,
    average: float | None,
) -> OrderSnapshot:
    return OrderSnapshot(
        id="core-1",
        source_module="paper_execution_venue_reconciliation",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id="client-1",
        status=status,
        filled_quantity=filled,
        remaining_quantity=1.0 - filled,
        average_fill_price=average,
        fees_paid=filled * 0.01,
        time_in_force=TimeInForce.GTC,
    )


def _event(
    *,
    event_id: str,
    event: str,
    status: OrderStatus,
    filled: float,
    average: float | None,
    seconds: int,
    reason: str | None = None,
) -> OrderEvent:
    return OrderEvent(
        timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=seconds),
        event=event,
        order=_snapshot(status=status, filled=filled, average=average),
        reason=reason,
        event_id=event_id,
        correlation_id="plan-1",
        causation_id="submit-1",
    )


def _aggregate() -> OrderAggregate:
    return OrderAggregate(
        client_order_id="client-1",
        core_order_id="core-1",
        requested_quantity=Decimal("1"),
        state=PaperOrderState.SUBMITTED,
        portfolio_id="portfolio-1",
        instrument="BTCUSDT",
        intent_role="entry",
    )


def test_exact_duplicate_remains_idempotent_after_newer_event() -> None:
    aggregate = _aggregate()
    partial = _event(
        event_id="event-partial",
        event="partially_filled",
        status=OrderStatus.PARTIALLY_FILLED,
        filled=0.4,
        average=100.0,
        seconds=1,
    )
    filled = _event(
        event_id="event-filled",
        event="filled",
        status=OrderStatus.FILLED,
        filled=1.0,
        average=101.0,
        seconds=2,
    )

    assert aggregate.apply_event(partial, 1)
    assert aggregate.apply_event(filled, 2)
    assert aggregate.apply_event(partial, 3) is False
    assert aggregate.state is PaperOrderState.FILLED
    assert aggregate.filled_quantity == Decimal("1.0")
    assert aggregate.last_sequence == 2


def test_conflicting_duplicate_fill_is_rejected_without_mutation() -> None:
    aggregate = _aggregate()
    original = _event(
        event_id="event-same",
        event="partially_filled",
        status=OrderStatus.PARTIALLY_FILLED,
        filled=0.4,
        average=100.0,
        seconds=1,
    )
    assert aggregate.apply_event(original, 1)
    before = (
        aggregate.state,
        aggregate.filled_quantity,
        aggregate.average_fill_price,
        aggregate.fees_paid,
        aggregate.last_sequence,
        dict(aggregate.processed_event_hashes),
    )

    conflicting = replace(
        original,
        order=_snapshot(
            status=OrderStatus.PARTIALLY_FILLED,
            filled=0.5,
            average=100.0,
        ),
    )
    with pytest.raises(OrderStateError, match="conflicting content"):
        aggregate.apply_event(conflicting, 2)
    assert (
        aggregate.state,
        aggregate.filled_quantity,
        aggregate.average_fill_price,
        aggregate.fees_paid,
        aggregate.last_sequence,
        aggregate.processed_event_hashes,
    ) == (*before[:-1], before[-1])


def test_timestamp_reason_and_correlation_are_part_of_fingerprint() -> None:
    aggregate = _aggregate()
    original = _event(
        event_id="event-context",
        event="submitted",
        status=OrderStatus.PENDING,
        filled=0.0,
        average=None,
        seconds=1,
        reason="initial",
    )
    assert aggregate.apply_event(original, 1)

    variants = (
        replace(original, timestamp=original.timestamp + timedelta(seconds=1)),
        replace(original, reason="changed"),
        replace(original, correlation_id="plan-2"),
        replace(original, causation_id="submit-2"),
    )
    for variant in variants:
        with pytest.raises(OrderStateError, match="conflicting content"):
            aggregate.apply_event(variant, 2)


def test_legacy_empty_fingerprint_fails_closed_on_duplicate() -> None:
    aggregate = _aggregate()
    aggregate.processed_event_ids.add("legacy-event")
    aggregate.processed_event_hashes["legacy-event"] = ""
    duplicate = _event(
        event_id="legacy-event",
        event="submitted",
        status=OrderStatus.PENDING,
        filled=0.0,
        average=None,
        seconds=1,
    )

    with pytest.raises(OrderStateError, match="conflicting content"):
        aggregate.apply_event(duplicate, 1)
