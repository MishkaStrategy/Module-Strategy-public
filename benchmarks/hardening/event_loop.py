from __future__ import annotations

import argparse
import json
import statistics
from datetime import datetime, timedelta, timezone
from time import perf_counter

from modular_core import (
    BacktestEngine,
    Candle,
    DataConfig,
    EngineConfig,
    MarketView,
    ModuleDecision,
    StrategyModule,
)


class Noop(StrategyModule):
    def __init__(self, index: int) -> None:
        self.name = f"noop_{index}"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def run(
    *, bars_count: int, modules_count: int, repeats: int, max_history: int | None
) -> dict[str, object]:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    bars = tuple(
        Candle(
            start + timedelta(minutes=5 * index),
            100 + (index % 100) * 0.01,
            101 + (index % 100) * 0.01,
            99 + (index % 100) * 0.01,
            100 + (index % 100) * 0.01,
            1,
            "BENCH",
            "5m",
        )
        for index in range(bars_count)
    )
    durations: list[float] = []
    run_ids: list[str] = []
    for _ in range(repeats):
        modules = tuple(Noop(index) for index in range(modules_count))
        engine = BacktestEngine(
            modules, EngineConfig(data=DataConfig(max_base_history=max_history))
        )
        before = perf_counter()
        result = engine.run(bars)
        durations.append(perf_counter() - before)
        run_ids.append(result.run_manifest.run_id)
    median = statistics.median(durations)
    calls = bars_count * modules_count
    return {
        "bars": bars_count,
        "modules": modules_count,
        "module_calls": calls,
        "repeats": repeats,
        "durations_seconds": durations,
        "median_seconds": median,
        "throughput_calls_per_second": calls / median,
        "deterministic": len(set(run_ids)) == 1,
        "run_id": run_ids[0],
        "max_base_history": max_history,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=20_000)
    parser.add_argument("--modules", type=int, default=10)
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--max-history", type=int, default=2_000)
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                bars_count=args.bars,
                modules_count=args.modules,
                repeats=args.repeats,
                max_history=args.max_history,
            ),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
