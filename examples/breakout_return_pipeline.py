"""Minimal standalone Breakout–Return backtest example."""

from datetime import datetime, timedelta, timezone

from modular_core import BacktestEngine, Candle
from strategy_modules.breakout_return import BreakoutReturnModule


def build_demo_bars() -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    prices = [
        (95, 100, 90, 96),
        (96, 99, 91, 95),
        (95, 98, 92, 94),
        (101, 104, 100.5, 103),
        (99.5, 101, 98.5, 99),
    ]
    return [
        Candle(
            start + timedelta(minutes=5 * index),
            open_,
            high,
            low,
            close,
            1.0,
            "BTCUSDT",
            "5m",
        )
        for index, (open_, high, low, close) in enumerate(prices)
    ]


module = BreakoutReturnModule(lookback_bars=3)
result = BacktestEngine(module).run(build_demo_bars())
for row in result.journal.signals:
    print(row["signal"])
