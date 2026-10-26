from datetime import datetime, timezone

import pytest

from modular_core import (
    CancelRequest,
    ClosePositionRequest,
    EngineConfig,
    OrderRequest,
    OrderStatus,
    OrderType,
    PositionSide,
    PositionUpdateRequest,
    Side,
)
from modular_core.audit import Journal
from modular_core.events import EventEnvelope, InMemoryEventStore
from modular_core.execution import BrokerSimulator
from modular_core.models import Position

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FailingStore(InMemoryEventStore):
    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.calls = 0

    def append(self, event: EventEnvelope) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise OSError("durable append failed")
        super().append(event)


def _broker(fail_on: int) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(),
        Journal(run_id="run-test", event_store=FailingStore(fail_on)),
    )


def _position() -> Position:
    return Position(
        id="pos-1",
        entry_order_id="ord-entry",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0,
        stop_loss=80,
        take_profit=120,
    )


def test_submit_does_not_commit_when_durable_event_fails() -> None:
    broker = _broker(1)
    with pytest.raises(OSError, match="durable append"):
        broker.submit(
            OrderRequest(Side.BUY, OrderType.MARKET, quantity=1),
            source_module="entry",
            created_at=NOW,
            current_index=0,
            mark_price=100,
        )
    assert broker.orders == []
    assert broker.pending_orders == []
    assert broker._order_sequence == 0


def test_update_and_scheduled_close_commit_only_after_journal_acceptance() -> None:
    update_broker = _broker(1)
    update_broker.portfolio.position = _position()
    with pytest.raises(OSError, match="durable append"):
        update_broker.update_position(
            PositionUpdateRequest(90, 130),
            timestamp=NOW,
            source_module="risk",
            mark_price=100,
        )
    assert update_broker.portfolio.position.stop_loss == 80
    assert update_broker.portfolio.position.take_profit == 120

    close_broker = _broker(1)
    close_broker.portfolio.position = _position()
    with pytest.raises(OSError, match="durable append"):
        close_broker.schedule_close(
            ClosePositionRequest("exit"),
            timestamp=NOW,
            current_index=0,
            source_module="exit",
        )
    assert close_broker.pending_close is None


def test_cancel_state_matches_last_durable_prefix_on_second_event_failure() -> None:
    broker = _broker(3)
    order = broker.submit(
        OrderRequest(Side.BUY, OrderType.LIMIT, quantity=1, price=90),
        source_module="entry",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )
    assert order is not None
    with pytest.raises(OSError, match="durable append"):
        broker.cancel(
            CancelRequest(order_id=order.id),
            timestamp=NOW,
            source_module="entry",
        )
    assert order.status is OrderStatus.CANCEL_PENDING
    assert broker.pending_orders == [order]
    assert [event.event for event in broker.journal.orders] == [
        "submitted",
        "cancel_pending",
    ]
