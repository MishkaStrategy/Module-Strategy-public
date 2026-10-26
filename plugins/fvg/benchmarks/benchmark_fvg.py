from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from time import perf_counter

from modular_core import Candle, MarketView
from modular_strategy_fvg import FvgModule


def run_benchmark(bar_count: int, duplicate_calls: int) -> tuple[float, int]:
    module = FvgModule(timeframes=["15m"], publish_lifecycle_events=False)
    series: list[Candle] = []
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    calls = 0
    started = perf_counter()
    for index in range(bar_count):
        candle = Candle(
            timestamp=start + timedelta(minutes=15 * index),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            symbol="BTCUSDT",
            timeframe="15m",
        )
        series.append(candle)
        base = Candle(
            timestamp=candle.close_time - timedelta(minutes=5),
            open=100,
            high=101,
            low=99,
            close=100,
            volume=1,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        view = MarketView(
            base_bars=(base,),
            timeframe_bars={"15m": series},
            index=index,
            cash=10_000,
            equity=10_000,
            position=None,
            pending_orders=(),
        )
        module.on_bar(view)
        calls += 1
        for _ in range(duplicate_calls):
            module.on_bar(view)
            calls += 1
    return perf_counter() - started, calls


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=20_000)
    parser.add_argument("--duplicate-calls", type=int, default=2)
    args = parser.parse_args()
    elapsed, calls = run_benchmark(args.bars, args.duplicate_calls)
    print(f"bars={args.bars} calls={calls} elapsed_seconds={elapsed:.6f}")
    print(f"calls_per_second={calls / elapsed:.2f}")


if __name__ == "__main__":
    main()
