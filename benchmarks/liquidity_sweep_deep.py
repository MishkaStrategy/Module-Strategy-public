#!/usr/bin/env python3
"""Deterministic Liquidity Sweep benchmark with throughput, memory and restore metrics."""

from __future__ import annotations

import argparse
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import random
import statistics
import time
import tracemalloc
from typing import Any

from modular_core import Candle, MarketView
from strategy_modules.liquidity_sweep import LiquiditySweepModule, MODULE_VERSION

START = datetime(2020, 1, 1, tzinfo=timezone.utc)
SEED = 20260721


def candle_stream(count: int) -> Iterator[Candle]:
    randomizer = random.Random(SEED)
    price = 100.0
    for index in range(count):
        price = max(1e-8, price + randomizer.uniform(-0.5, 0.5))
        open_price = max(1e-8, price + randomizer.uniform(-0.15, 0.15))
        close_price = max(1e-8, price + randomizer.uniform(-0.15, 0.15))
        high = max(open_price, close_price) + randomizer.uniform(0.01, 0.7)
        low = max(1e-9, min(open_price, close_price) - randomizer.uniform(0.01, 0.7))
        yield Candle(
            START + timedelta(minutes=5 * index),
            open_price,
            high,
            low,
            close_price,
            1.0,
            "BTCUSDT",
            "5m",
        )


def run_once(count: int, *, measure_memory: bool = False) -> dict[str, Any]:
    module = LiquiditySweepModule(max_active_levels=128)
    if measure_memory:
        tracemalloc.start()
    started = time.perf_counter()
    for index, candle in enumerate(candle_stream(count)):
        module.on_bar(MarketView((candle,), {}, index, 10_000, 10_000, None, ()))
    elapsed = time.perf_counter() - started
    peak_bytes: int | None = None
    if measure_memory:
        _, peak_bytes = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    state = module.snapshot_state()
    checkpoint_bytes = len(json.dumps(state, separators=(",", ":")).encode("utf-8"))
    restore_samples: list[float] = []
    for _ in range(25):
        restored = LiquiditySweepModule(max_active_levels=128)
        restore_started = time.perf_counter()
        restored.restore_state(state)
        restore_samples.append(time.perf_counter() - restore_started)
    return {
        "seconds": elapsed,
        "throughput_events_per_second": count / elapsed,
        "peak_bytes": peak_bytes,
        "checkpoint_bytes": checkpoint_bytes,
        "restore_median_seconds": statistics.median(restore_samples),
        "active_levels": len(state["levels"]),
        "buffered_bars": len(state["bars"]),
    }


def summarize(count: int, runs: int) -> dict[str, Any]:
    samples = [run_once(count) for _ in range(runs)]
    seconds = [float(sample["seconds"]) for sample in samples]
    throughput = [float(sample["throughput_events_per_second"]) for sample in samples]
    return {
        "events": count,
        "runs": runs,
        "median_seconds": statistics.median(seconds),
        "minimum_seconds": min(seconds),
        "maximum_seconds": max(seconds),
        "median_throughput_events_per_second": statistics.median(throughput),
        "samples": samples,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sizes", default="1000:5,50000:5,250000:3")
    parser.add_argument("--memory-events", type=int, default=50_000)
    parser.add_argument("--stress-events", type=int, default=0)
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    workloads: list[dict[str, Any]] = []
    for item in args.sizes.split(","):
        count_text, runs_text = item.split(":", 1)
        workloads.append(summarize(int(count_text), int(runs_text)))
    payload: dict[str, Any] = {
        "module_version": MODULE_VERSION,
        "python": __import__("sys").version,
        "seed": SEED,
        "workloads": workloads,
        "memory_workload": {
            "events": args.memory_events,
            **run_once(args.memory_events, measure_memory=True),
        },
    }
    if args.stress_events:
        payload["stress_workload"] = {
            "events": args.stress_events,
            **run_once(args.stress_events),
        }
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
