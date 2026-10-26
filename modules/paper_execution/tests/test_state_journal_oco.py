from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from modular_core import OrderEvent, OrderSnapshot, OrderStatus, OrderType, Side

from paper_execution import (
    ExecutionEventJournal,
    OcoGroup,
    OrderAggregate,
    OrderStateError,
    PaperOrderState,
    PaperReasonCode,
)


def snapshot(
    now,
    *,
    status=OrderStatus.PENDING,
    filled=0.0,
    remaining=1.0,
    average=None,
):
    return OrderSnapshot(
        id="ord-1",
        source_module="paper_execution_venue_reconciliation",
        created_at=now,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        price=None,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id="client-1",
        status=status,
        filled_quantity=filled,
        remaining_quantity=remaining,
        average_fill_price=average,
    )


def event(now, **kwargs):
    return OrderEvent(
        timestamp=now,
        event="updated",
        order=snapshot(now, **kwargs),
    )


def test_state_machine_partial_then_full(now) -> None:
    aggregate = OrderAggregate(
        "client-1",
        "ord-1",
        Decimal("1"),
        PaperOrderState.SUBMITTED,
    )
    assert aggregate.apply_core_event(
        event(
            now,
            status=OrderStatus.PARTIALLY_FILLED,
            filled=0.4,
            remaining=0.6,
            average=100.0,
        ),
        sequence=1,
        event_id="e1",
    )
    assert aggregate.state is PaperOrderState.PARTIALLY_FILLED
    assert aggregate.apply_core_event(
        event(
            now,
            status=OrderStatus.FILLED,
            filled=1.0,
            remaining=0.0,
            average=101.0,
        ),
        sequence=2,
        event_id="e2",
    )
    assert aggregate.state is PaperOrderState.FILLED
    assert aggregate.remaining_quantity == 0


def test_duplicate_event_is_idempotent(now) -> None:
    aggregate = OrderAggregate(
        "client-1",
        "ord-1",
        Decimal("1"),
        PaperOrderState.SUBMITTED,
    )
    core_event = event(now)
    assert aggregate.apply_core_event(
        core_event,
        sequence=1,
        event_id="same",
    )
    assert not aggregate.apply_core_event(
        core_event,
        sequence=1,
        event_id="same",
    )


def test_out_of_order_event_is_rejected(now) -> None:
    aggregate = OrderAggregate(
        "client-1",
        "ord-1",
        Decimal("1"),
        PaperOrderState.SUBMITTED,
    )
    aggregate.apply_core_event(event(now), sequence=2, event_id="e2")
    with pytest.raises(OrderStateError, match="out-of-order"):
        aggregate.apply_core_event(
            event(now),
            sequence=1,
            event_id="e1",
        )


def test_terminal_state_cannot_reopen(now) -> None:
    aggregate = OrderAggregate(
        "client-1",
        "ord-1",
        Decimal("1"),
        PaperOrderState.SUBMITTED,
    )
    aggregate.apply_core_event(
        event(
            now,
            status=OrderStatus.FILLED,
            filled=1.0,
            remaining=0.0,
            average=100.0,
        ),
        sequence=1,
        event_id="e1",
    )
    reopened = replace(
        event(now).order,
        status=OrderStatus.PENDING,
        filled_quantity=1.0,
        remaining_quantity=0.0,
        average_fill_price=100.0,
    )
    with pytest.raises(OrderStateError, match="invalid order transition"):
        aggregate.apply_core_event(
            OrderEvent(
                timestamp=now,
                event="reopen",
                order=reopened,
            ),
            sequence=2,
            event_id="e2",
        )


def test_overfill_is_blocking_invariant(now) -> None:
    aggregate = OrderAggregate(
        "client-1",
        "ord-1",
        Decimal("1"),
        PaperOrderState.SUBMITTED,
    )
    with pytest.raises(OrderStateError) as exc:
        aggregate.apply_core_event(
            event(
                now,
                status=OrderStatus.FILLED,
                filled=1.1,
                remaining=0.0,
            ),
            sequence=1,
            event_id="e1",
        )
    assert exc.value.reason_code is PaperReasonCode.INVARIANT_VIOLATION


def test_journal_chain_is_deterministic_and_valid(now) -> None:
    first = ExecutionEventJournal("run-1")
    second = ExecutionEventJournal("run-1")
    for journal in (first, second):
        journal.append(
            timestamp=now,
            event_type="plan.accepted",
            aggregate_id="p",
            payload={"x": Decimal("1.2")},
        )
        journal.append(
            timestamp=now,
            event_type="order.submitted",
            aggregate_id="o",
            payload={"y": 2},
        )
        assert journal.verify_chain()
    assert first.store.read() == second.store.read()


def test_oco_full_fill_cancels_siblings() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target", Decimal("1"))
    assert group.record_fill("target", Decimal("1")) == ("stop",)


def test_oco_prevents_over_close() -> None:
    group = OcoGroup("g", Decimal("1"))
    group.register("stop", Decimal("1"))
    group.register("target", Decimal("1"))
    group.record_fill("target", Decimal("0.6"))
    with pytest.raises(OrderStateError, match="over-close"):
        group.record_fill("stop", Decimal("0.5"))


def test_journal_does_not_require_core_042_entity_key(now) -> None:
    journal = ExecutionEventJournal("base-compatible")
    journal_event = journal.append(
        timestamp=now,
        event_type="plan.accepted",
        aggregate_id="plan-base",
        payload={"value": "ok"},
    )
    assert journal_event.payload["aggregate_id"] == "plan-base"
    assert journal.verify_chain()
