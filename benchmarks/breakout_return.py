"""Repeatable hot-path comparison for BreakoutReturnModule rolling extrema."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from time import perf_counter

from modular_core import Candle, MarketView
from strategy_modules.breakout_return import BreakoutReturnModule


def build_bars(count: int) -> list[Candle]:
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    bars: list[Candle] = []
    price = 100.0
    for index in range(count):
        price += ((index % 17) - 8) * 0.0005
        bars.append(
            Candle(
                start + timedelta(minutes=5 * index),
                price,
                price + 0.5 + (index % 5) * 0.01,
                price - 0.5 - (index % 7) * 0.01,
                price + ((index % 3) - 1) * 0.01,
                1.0,
                "BTCUSDT",
                "5m",
            )
        )
    return bars


def optimized(bars: list[Candle], lookback: int) -> float:
    module = BreakoutReturnModule(
        lookback_bars=lookback,
        direction="above",
        breakout_buffer_bps=5000,
    )
    visible: list[Candle] = []
    started = perf_counter()
    for index, bar in enumerate(bars):
        visible.append(bar)
        module.on_bar(
            MarketView(
                base_bars=visible,
                timeframe_bars={},
                index=index,
                cash=10_000,
                equity=10_000,
                position=None,
                pending_orders=(),
            )
        )
    return perf_counter() - started


def naive(bars: list[Candle], lookback: int) -> float:
    started = perf_counter()
    checksum = 0.0
    for index in range(lookback, len(bars)):
        window = bars[index - lookback : index]
        checksum += max(item.high for item in window)
        checksum -= min(item.low for item in window)
    elapsed = perf_counter() - started
    if checksum == float("inf"):
        raise AssertionError("unreachable")
    return elapsed


if __name__ == "__main__":
    count = 30_000
    lookback = 1_000
    bars = build_bars(count)
    optimized_seconds = optimized(bars, lookback)
    naive_seconds = naive(bars, lookback)
    print(f"bars={count} lookback={lookback}")
    print(f"optimized_module_seconds={optimized_seconds:.6f}")
    print(f"naive_extrema_seconds={naive_seconds:.6f}")
    print(f"naive_over_optimized={naive_seconds / optimized_seconds:.2f}x")
