from __future__ import annotations

import argparse
import json
import resource
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
    ModuleManifest,
    StrategyModule,
    TimeframeSpec,
)


class Probe(StrategyModule):
    manifest = ModuleManifest(name="probe", required_timeframes=("15m", "1h"))

    def __init__(self, name: str) -> None:
        self.name = name
        self.manifest = ModuleManifest(name=name, required_timeframes=("15m", "1h"))
        self.max_base = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.max_base = max(self.max_base, len(view.base_bars))
        return ModuleDecision()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=50_000)
    parser.add_argument("--modules", type=int, default=5)
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--history", type=int, default=512)
    args = parser.parse_args()
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    bars = tuple(
        Candle(
            start + timedelta(minutes=5 * index),
            100,
            101,
            99,
            100 + (index % 11) * 0.001,
            1,
            "BTCUSDT",
            "5m",
        )
        for index in range(args.bars)
    )
    durations = []
    run_ids = []
    checkpoint_sizes = []
    for _ in range(args.runs):
        modules = tuple(Probe(f"probe_{index}") for index in range(args.modules))
        engine = BacktestEngine(
            modules,
            EngineConfig(data=DataConfig(max_base_history=args.history)),
            timeframes=(
                TimeframeSpec(3, "15m", max_history=256),
                TimeframeSpec(12, "1h", max_history=128),
            ),
        )
        began = perf_counter()
        result = engine.run(bars)
        durations.append(perf_counter() - began)
        run_ids.append(result.run_manifest.run_id)
        checkpoint_sizes.append(len(json.dumps(result.checkpoint, default=str)))
        assert all(module.max_base <= args.history for module in modules)
    report = {
        "bars": args.bars,
        "modules": args.modules,
        "timeframes": ["5m", "15m", "1h"],
        "runs": args.runs,
        "durations_seconds": durations,
        "median_seconds": statistics.median(durations),
        "throughput_module_calls_per_second": args.bars
        * args.modules
        / statistics.median(durations),
        "peak_rss_kib": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "checkpoint_size_bytes": checkpoint_sizes,
        "deterministic_run_id": len(set(run_ids)) == 1,
        "run_id": run_ids[0],
        "base_history_limit": args.history,
    }
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
