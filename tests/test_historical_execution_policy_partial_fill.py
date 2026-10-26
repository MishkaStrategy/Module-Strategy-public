from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    OrderRequest,
    OrderType,
    Side,
)
from modular_core.audit import Journal
from modular_core.execution import BrokerSimulator
from modular_core.models import OrderStatus


def _bar(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
        open_price,
        high,
        low,
        close,
        1.0,
        "BTCUSDT",
        "5m",
    )


def test_fill_bar_stop_cancels_partial_limit_remainder_and_prevents_reentry() -> None:
    journal = Journal(run_id="historical-partial-fill-stop-test")
    broker = BrokerSimulator(
        EngineConfig(
            execution=ExecutionConfig(
                allow_limit_stop_on_entry_bar=True,
                max_fill_fraction_per_bar=0.5,
            )
        ),
        journal,
    )
    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=2.0,
            price=100.0,
            stop_loss=95.0,
            take_profit=110.0,
        ),
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )

    assert order is not None
    broker.process_bar(
        _bar(1, open_price=100.0, high=101.0, low=94.0, close=96.0),
        1,
    )

    assert broker.portfolio.position is None
    assert order.filled_quantity == 1.0
    assert order.remaining_quantity == 1.0
    assert order.status is OrderStatus.CANCELLED
    assert broker.pending_orders == []
    assert len(journal.fills) == 2
    assert len(journal.trades) == 1
    assert journal.trades[0].exit_reason == "stop_on_fill_bar"
    assert journal.orders[-1].event == "cancelled"
    assert journal.orders[-1].reason == "stop_on_fill_bar_remainder"

    broker.process_bar(
        _bar(2, open_price=100.0, high=102.0, low=99.0, close=101.0),
        2,
    )

    assert broker.portfolio.position is None
    assert order.filled_quantity == 1.0
    assert broker.pending_orders == []
    assert len(journal.fills) == 2
    assert len(journal.trades) == 1
