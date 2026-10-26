"""Small repeatable micro-benchmark; not a correctness test."""

from datetime import UTC, datetime, timedelta
from time import perf_counter

from modular_core import (
    BacktestEngine,
    Candle,
    MarketView,
    ModuleDecision,
    StrategyModule,
)


class Noop(StrategyModule):
    def __init__(self, name: str) -> None:
        self.name = name

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


bars = [
    Candle(
        datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * i),
        100,
        101,
        99,
        100,
        1,
        "BTCUSDT",
        "5m",
    )
    for i in range(20_000)
]
engine = BacktestEngine(tuple(Noop(f"noop_{i}") for i in range(10)))
started = perf_counter()
engine.run(bars)
print(f"20k bars / 10 modules: {perf_counter() - started:.4f}s")
