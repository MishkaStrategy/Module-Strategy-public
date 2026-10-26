from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, cast

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
from paper_execution.journal import ExecutionEventJournal
from paper_execution.recovery import restore_execution_state, snapshot_execution_state
from paper_execution.state_machine import OrderAggregate


_BASE_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _snapshot(
    *,
    status: OrderStatus,
    filled: float,
    average: float | None,
) -> OrderSnapshot:
    return OrderSnapshot(
        id="core-1",
        source_module="paper_execution_venue_reconciliation",
        created_at=_BASE_TIME,
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
        fees_paid=max(filled, 0.0) * 0.01,
        time_in_force=TimeInForce.GTC,
    )


def _event(
    *,
    event_id: str,
    seconds: int,
    status: OrderStatus = OrderStatus.PENDING,
    filled: float = 0.0,
    average: float | None = None,
) -> OrderEvent:
    return OrderEvent(
        timestamp=_BASE_TIME + timedelta(seconds=seconds),
        event=status.value,
        order=_snapshot(status=status, filled=filled, average=average),
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


def test_journal_rejects_decreasing_timestamp_without_mutation() -> None:
    journal = ExecutionEventJournal("run-timestamp")
    journal.append(
        timestamp=_BASE_TIME + timedelta(seconds=2),
        event_type="order.first",
        aggregate_id="order-1",
        payload={"value": 1},
    )
    with pytest.raises(ValueError, match="precedes previous"):
        journal.append(
            timestamp=_BASE_TIME + timedelta(seconds=1),
            event_type="order.stale",
            aggregate_id="order-1",
            payload={"value": 2},
        )
    assert journal.sequence == 1
    assert len(tuple(journal.store.read())) == 1
    assert journal.verify_chain()


def test_order_rejects_new_sequence_with_stale_timestamp_without_mutation() -> None:
    aggregate = _aggregate()
    first = _event(event_id="first", seconds=10)
    assert aggregate.apply_event(first, 1)
    before = (
        aggregate.state,
        aggregate.last_sequence,
        aggregate.last_event_timestamp,
        set(aggregate.processed_event_ids),
    )
    stale = _event(
        event_id="stale",
        seconds=9,
        status=OrderStatus.PARTIALLY_FILLED,
        filled=0.4,
        average=100.0,
    )
    with pytest.raises(OrderStateError, match="stale order event timestamp"):
        aggregate.apply_event(stale, 2)
    assert (
        aggregate.state,
        aggregate.last_sequence,
        aggregate.last_event_timestamp,
        aggregate.processed_event_ids,
    ) == before


def test_equal_order_event_timestamp_is_allowed() -> None:
    aggregate = _aggregate()
    assert aggregate.apply_event(_event(event_id="first", seconds=10), 1)
    assert aggregate.apply_event(
        _event(
            event_id="partial",
            seconds=10,
            status=OrderStatus.PARTIALLY_FILLED,
            filled=0.4,
            average=100.0,
        ),
        2,
    )
    assert aggregate.last_event_timestamp == _BASE_TIME + timedelta(seconds=10)


def test_recovery_v4_preserves_timestamp_and_stale_rejection() -> None:
    aggregate = _aggregate()
    assert aggregate.apply_event(_event(event_id="first", seconds=10), 1)
    state = snapshot_execution_state(
        configuration_hash="a" * 64,
        plan_results={},
        plan_scopes={},
        orders={aggregate.client_order_id: aggregate},
        blocked_scopes=set(),
        event_sequence=1,
        journal_attestation={},
    )
    restored = restore_execution_state(
        state,
        expected_configuration_hash="a" * 64,
    ).orders[aggregate.client_order_id]
    assert restored.last_event_timestamp == aggregate.last_event_timestamp
    with pytest.raises(OrderStateError, match="stale order event timestamp"):
        restored.apply_event(
            _event(event_id="stale-after-restart", seconds=9),
            2,
        )


def test_order_identity_mismatch_is_rejected() -> None:
    aggregate = _aggregate()
    event = _event(event_id="identity", seconds=1)
    event = replace(event, order=replace(event.order, id="other-core"))
    with pytest.raises(OrderStateError, match="identity mismatch"):
        aggregate.apply_event(event, 1)


def test_inconsistent_remaining_quantity_is_rejected() -> None:
    aggregate = _aggregate()
    event = _event(
        event_id="remaining",
        seconds=1,
        status=OrderStatus.PARTIALLY_FILLED,
        filled=0.4,
        average=100.0,
    )
    event = replace(event, order=replace(event.order, remaining_quantity=0.7))
    with pytest.raises(OrderStateError, match="quantities are inconsistent"):
        aggregate.apply_event(event, 1)


def test_negative_fees_are_rejected() -> None:
    aggregate = _aggregate()
    event = _event(event_id="fees", seconds=1)
    event = replace(event, order=replace(event.order, fees_paid=-1.0))
    with pytest.raises(OrderStateError, match="fees cannot be negative"):
        aggregate.apply_event(event, 1)


def test_unfilled_average_price_is_rejected() -> None:
    aggregate = _aggregate()
    event = _event(event_id="average", seconds=1)
    event = replace(event, order=replace(event.order, average_fill_price=100.0))
    with pytest.raises(OrderStateError, match="unfilled order"):
        aggregate.apply_event(event, 1)


def test_filled_quantity_requires_average_price() -> None:
    aggregate = _aggregate()
    event = _event(
        event_id="missing-average",
        seconds=1,
        status=OrderStatus.PARTIALLY_FILLED,
        filled=0.4,
        average=100.0,
    )
    event = replace(event, order=replace(event.order, average_fill_price=None))
    with pytest.raises(OrderStateError, match="requires average fill price"):
        aggregate.apply_event(event, 1)


def test_unsupported_core_status_is_rejected() -> None:
    aggregate = _aggregate()
    event = _event(event_id="status", seconds=1)
    event = replace(event, order=replace(event.order, status=cast(Any, "unknown")))
    with pytest.raises(OrderStateError, match="unsupported Core order status"):
        aggregate.apply_event(event, 1)


def test_partial_state_requires_strict_partial_quantity() -> None:
    aggregate = _aggregate()
    event = _event(event_id="partial-zero", seconds=1)
    event = replace(
        event,
        order=replace(event.order, status=OrderStatus.PARTIALLY_FILLED),
    )
    with pytest.raises(OrderStateError, match="invalid cumulative quantity"):
        aggregate.apply_event(event, 1)


@pytest.mark.parametrize("filled", [Decimal("-0.1"), Decimal("1.1")])
def test_constructor_rejects_invalid_filled_quantity(filled: Decimal) -> None:
    with pytest.raises(ValueError, match="filled quantity"):
        OrderAggregate(
            client_order_id="client-1",
            core_order_id="core-1",
            requested_quantity=Decimal("1"),
            filled_quantity=filled,
        )


def test_constructor_rejects_negative_fees() -> None:
    with pytest.raises(ValueError, match="fees_paid"):
        OrderAggregate(
            client_order_id="client-1",
            core_order_id="core-1",
            requested_quantity=Decimal("1"),
            fees_paid=Decimal("-0.01"),
        )


def test_constructor_rejects_negative_sequence() -> None:
    with pytest.raises(ValueError, match="last_sequence"):
        OrderAggregate(
            client_order_id="client-1",
            core_order_id="core-1",
            requested_quantity=Decimal("1"),
            last_sequence=-1,
        )


def test_constructor_rejects_timestamp_without_processed_event() -> None:
    with pytest.raises(ValueError, match="timestamp requires"):
        OrderAggregate(
            client_order_id="client-1",
            core_order_id="core-1",
            requested_quantity=Decimal("1"),
            last_event_timestamp=_BASE_TIME,
        )


def test_constructor_rejects_mismatched_event_fingerprints() -> None:
    with pytest.raises(ValueError, match="fingerprints do not match"):
        OrderAggregate(
            client_order_id="client-1",
            core_order_id="core-1",
            requested_quantity=Decimal("1"),
            processed_event_ids={"event-1"},
            processed_event_hashes={},
        )


def test_blank_explicit_event_id_is_rejected() -> None:
    aggregate = _aggregate()
    with pytest.raises(OrderStateError, match="event_id must be non-blank"):
        aggregate.apply_core_event(
            _event(event_id="ignored", seconds=1),
            sequence=1,
            event_id=" ",
        )


def test_cumulative_fill_cannot_decrease() -> None:
    aggregate = _aggregate()
    assert aggregate.apply_event(
        _event(
            event_id="partial-high",
            seconds=1,
            status=OrderStatus.PARTIALLY_FILLED,
            filled=0.6,
            average=100.0,
        ),
        1,
    )
    with pytest.raises(OrderStateError, match="cumulative fill decreased"):
        aggregate.apply_event(
            _event(
                event_id="partial-low",
                seconds=2,
                status=OrderStatus.PARTIALLY_FILLED,
                filled=0.4,
                average=100.0,
            ),
            2,
        )


def test_fill_cannot_exceed_requested_quantity() -> None:
    aggregate = _aggregate()
    event = _event(
        event_id="overfill",
        seconds=1,
        status=OrderStatus.FILLED,
        filled=1.0,
        average=100.0,
    )
    event = replace(
        event,
        order=replace(
            event.order,
            filled_quantity=1.1,
            remaining_quantity=-0.1,
        ),
    )
    with pytest.raises(OrderStateError, match="exceeds requested"):
        aggregate.apply_event(event, 1)


def test_negative_remaining_quantity_is_rejected() -> None:
    aggregate = _aggregate()
    event = _event(
        event_id="negative-remaining",
        seconds=1,
        status=OrderStatus.PARTIALLY_FILLED,
        filled=0.4,
        average=100.0,
    )
    event = replace(event, order=replace(event.order, remaining_quantity=-0.1))
    with pytest.raises(OrderStateError, match="quantities are inconsistent"):
        aggregate.apply_event(event, 1)


def test_filled_state_requires_full_quantity() -> None:
    aggregate = _aggregate()
    event = _event(
        event_id="short-filled",
        seconds=1,
        status=OrderStatus.FILLED,
        filled=0.9,
        average=100.0,
    )
    with pytest.raises(OrderStateError, match="requires full cumulative"):
        aggregate.apply_event(event, 1)
