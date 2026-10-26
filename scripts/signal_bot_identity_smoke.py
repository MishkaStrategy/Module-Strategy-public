from datetime import datetime, timezone
from decimal import Decimal

from modular_core import Side
from signal_bot.domain import TradeSignal

print(
    TradeSignal.stable_id(
        strategy_id="identity",
        strategy_version="1.0.0",
        symbol="BTCUSDT",
        side=Side.BUY,
        timeframe="5m",
        timestamp=datetime(2026, 7, 22, tzinfo=timezone.utc),
        entry=Decimal("100.0"),
        setup_id="setup-1",
    )
)
