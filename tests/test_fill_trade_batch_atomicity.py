from datetime import datetime, timezone

import pytest

from modular_core import EngineConfig, PositionSide, Side
from modular_core.audit import Journal
from modular_core.events import EventEnvelope, InMemoryEventStore
from modular_core.execution import BrokerSimulator
from modular_core.models import Fill, Position

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FailingBatchStore(InMemoryEventStore):
    def append_many(self, events):  # type: ignore[no-untyped-def]
        batch = tuple(events)
        if len(batch) > 1:
            raise OSError("batch append failed")
        super().append_many(batch)


def test_close_position_does_not_mutate_when_fill_trade_batch_fails() -> None:
    journal = Journal(run_id="run-test", event_store=FailingBatchStore())
    broker = BrokerSimulator(EngineConfig(), journal)
    position = Position(
        id="pos-1",
        entry_order_id="ord-1",
        side=PositionSide.LONG,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        entry_index=0,
        entry_fee=1,
        stop_loss=90,
        take_profit=120,
        entry_fills=[
            Fill(
                order_id="ord-1",
                timestamp=NOW,
                side=Side.BUY,
                quantity=1,
                price=100,
                fee=1,
                slippage=0,
                reason="entry",
                liquidity="maker",
            )
        ],
    )
    broker.portfolio.position = position
    starting_cash = broker.portfolio.cash

    with pytest.raises(OSError, match="batch append"):
        broker.close_position(NOW, 110, "test", index=1)

    assert broker.portfolio.cash == starting_cash
    assert broker.portfolio.position is position
    assert position.quantity == 1
    assert position.realised_pnl == 0
    assert position.exit_fills == []
    assert journal.fills == []
    assert journal.trades == []
    assert broker._fill_sequence == 0


def test_in_memory_batch_validation_is_atomic() -> None:
    store = InMemoryEventStore()
    first = EventEnvelope("e1", "run", 1, NOW, "one", "test", {})
    duplicate = EventEnvelope("e1", "run", 2, NOW, "two", "test", {})
    with pytest.raises(ValueError, match="duplicate"):
        store.append_many((first, duplicate))
    assert store.read() == ()
