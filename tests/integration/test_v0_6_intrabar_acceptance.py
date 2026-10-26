from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from modular_core import Candle, EngineConfig, ExecutionConfig, Journal, Side
from modular_core.config import FeeConfig
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec
from modular_core.models import Fill, Position, PositionSide

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def test_same_bar_stop_and_target_uses_declared_conservative_stop_policy() -> None:
    broker = BrokerSimulator(
        EngineConfig(
            fees=FeeConfig(0, 0),
            execution=ExecutionConfig(
                intrabar_priority="stop",
                stop_slippage_bps=0,
            ),
        ),
        Journal(),
        instrument=InstrumentSpec(symbol="BTCUSDT"),
    )
    broker.portfolio.position = Position(
        id="position-intrabar-v06",
        entry_order_id="entry-intrabar-v06",
        side=PositionSide.LONG,
        quantity=1.0,
        entry_price=100.0,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0.0,
        stop_loss=95.0,
        take_profit=105.0,
        entry_fills=[
            Fill(
                order_id="entry-intrabar-v06",
                timestamp=NOW,
                side=Side.BUY,
                quantity=1.0,
                price=100.0,
                fee=0.0,
                slippage=0.0,
                reason="entry",
                liquidity="maker",
            )
        ],
    )

    broker.process_bar(
        Candle(
            timestamp=NOW + timedelta(minutes=5),
            open=100.0,
            high=106.0,
            low=94.0,
            close=100.0,
            volume=10.0,
            symbol="BTCUSDT",
            timeframe="5m",
        ),
        1,
    )

    trade = broker.journal.trades[-1]
    assert trade.exit_reason == "stop"
    assert trade.exit_price == pytest.approx(95.0)
    assert broker.portfolio.position is None
