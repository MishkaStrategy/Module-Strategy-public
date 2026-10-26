from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import time
import tracemalloc
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from modular_core import Candle, MarketView
from modular_strategy_fvg import FvgModule, __version__

UTC = timezone.utc
START = datetime(2020, 1, 1, tzinfo=UTC)


def bar(index: int, *, rising: bool = False) -> Candle:
    value = 100.0 + (index * 3.0 if rising else 0.0)
    return Candle(
        timestamp=START + timedelta(minutes=15 * index),
        open=value + (1.0 if rising else 0.0),
        high=value + (2.0 if rising else 1.0),
        low=value,
        close=value + (1.0 if rising else 0.0),
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="15m",
    )


def view(tail: tuple[Candle, ...], index: int, *, data_gap: bool = False) -> MarketView:
    latest = tail[-1]
    base = Candle(
        timestamp=latest.close_time - timedelta(minutes=5),
        open=100,
        high=101,
        low=99,
        close=100,
        volume=1,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketView(
        base_bars=(base,),
        timeframe_bars={"15m": tail},
        index=index,
        cash=10_000,
        equity=10_000,
        position=None,
        pending_orders=(),
        data_gap=data_gap,
    )


def run_flat(events: int, *, duplicate_calls: int = 0, trace_memory: bool = False) -> dict[str, Any]:
    module = FvgModule(timeframes=("15m",), publish_lifecycle_events=False)
    tail: list[Candle] = []
    calls = 0
    if trace_memory:
        tracemalloc.start()
    started = time.perf_counter()
    for index in range(events):
        tail.append(bar(index))
        if len(tail) > 3:
            tail.pop(0)
        market_view = view(tuple(tail), index)
        module.on_bar(market_view)
        calls += 1
        for _ in range(duplicate_calls):
            module.on_bar(market_view)
            calls += 1
    elapsed = time.perf_counter() - started
    peak = None
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    state_json = json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":"))
    return {
        "events": events,
        "calls": calls,
        "seconds": elapsed,
        "throughput_calls_per_second": calls / elapsed,
        "peak_memory_bytes": peak,
        "active_zones": len(module.active_zones),
        "checkpoint_bytes": len(state_json.encode("utf-8")),
    }


def summarize(runs: list[dict[str, Any]]) -> dict[str, Any]:
    seconds = [float(run["seconds"]) for run in runs]
    throughput = [float(run["throughput_calls_per_second"]) for run in runs]
    return {
        "runs": len(runs),
        "events": runs[0]["events"],
        "calls": runs[0]["calls"],
        "seconds": {
            "minimum": min(seconds),
            "median": statistics.median(seconds),
            "maximum": max(seconds),
            "all": seconds,
        },
        "throughput_calls_per_second": {
            "minimum": min(throughput),
            "median": statistics.median(throughput),
            "maximum": max(throughput),
        },
        "checkpoint_bytes": runs[-1]["checkpoint_bytes"],
        "active_zones": runs[-1]["active_zones"],
    }


def restart_digest(events: int = 50_000) -> dict[str, Any]:
    split = events // 2
    uninterrupted = FvgModule(
        timeframes=("15m",),
        publish_lifecycle_events=False,
        max_active_zones_per_timeframe=64,
    )
    restarted = FvgModule(
        timeframes=("15m",),
        publish_lifecycle_events=False,
        max_active_zones_per_timeframe=64,
    )
    tail: list[Candle] = []
    checkpoint_size = 0
    restore_ns = 0
    for index in range(events):
        # Periodic rising bars create zones; flat resets avoid an unrealistic all-zone workload.
        cycle = index % 12
        value = 100.0 + (cycle * 3.0 if cycle < 6 else 0.0)
        candle = Candle(
            timestamp=START + timedelta(minutes=15 * index),
            open=value + 1,
            high=value + 2,
            low=max(0.01, value),
            close=value + 1,
            volume=1,
            symbol="BTCUSDT",
            timeframe="15m",
        )
        tail.append(candle)
        if len(tail) > 3:
            tail.pop(0)
        market_view = view(tuple(tail), index)
        uninterrupted.on_bar(market_view)
        restarted.on_bar(market_view)
        if index + 1 == split:
            state = restarted.snapshot_state()
            encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode("utf-8")
            checkpoint_size = len(encoded)
            replacement = FvgModule(
                timeframes=("15m",),
                publish_lifecycle_events=False,
                max_active_zones_per_timeframe=64,
            )
            started = time.perf_counter_ns()
            replacement.restore_state(state)
            restore_ns = time.perf_counter_ns() - started
            restarted = replacement
    first = json.dumps(uninterrupted.snapshot_state(), sort_keys=True, separators=(",", ":")).encode()
    second = json.dumps(restarted.snapshot_state(), sort_keys=True, separators=(",", ":")).encode()
    return {
        "events": events,
        "split_at": split,
        "digest_uninterrupted": hashlib.sha256(first).hexdigest(),
        "digest_restarted": hashlib.sha256(second).hexdigest(),
        "digests_match": first == second,
        "checkpoint_bytes_at_split": checkpoint_size,
        "restore_latency_ns": restore_ns,
        "active_zones_final": len(restarted.active_zones),
    }


def restore_distribution(iterations: int = 1000) -> dict[str, Any]:
    module = FvgModule(timeframes=("15m",), publish_lifecycle_events=False)
    rising = tuple(bar(index, rising=True) for index in range(3))
    module.on_bar(view(rising, 2))
    state = module.snapshot_state()
    encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
    latencies: list[int] = []
    for _ in range(iterations):
        target = FvgModule(timeframes=("15m",), publish_lifecycle_events=False)
        started = time.perf_counter_ns()
        target.restore_state(state)
        latencies.append(time.perf_counter_ns() - started)
    return {
        "iterations": iterations,
        "checkpoint_bytes": len(encoded),
        "latency_ns": {
            "minimum": min(latencies),
            "median": int(statistics.median(latencies)),
            "maximum": max(latencies),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reproducible FVG performance and restart benchmark")
    parser.add_argument("--ci", action="store_true", help="Run a bounded CI checkpoint workload")
    parser.add_argument("--stress-events", type=int, default=0, help="Optional explicit stress workload")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "artifacts" / "benchmark_results.json",
    )
    args = parser.parse_args()

    if args.ci:
        smoke_runs, standard_runs = 3, 3
        standard_events, extended_events = 10_000, 50_000
        memory_events, restart_events, restore_iterations = 10_000, 10_000, 250
    else:
        smoke_runs, standard_runs = 5, 3
        standard_events, extended_events = 50_000, 250_000
        memory_events, restart_events, restore_iterations = 50_000, 50_000, 1000

    result: dict[str, Any] = {
        "python": sys.version.split()[0],
        "module_version": __version__,
        "workload_note": (
            "Rolling three-bar MarketView keeps external test-harness history bounded and "
            "measures module hot-path complexity."
        ),
        "smoke_1k": summarize([run_flat(1_000) for _ in range(smoke_runs)]),
        "standard": summarize([run_flat(standard_events) for _ in range(standard_runs)]),
        "standard_with_two_duplicates": summarize(
            [run_flat(standard_events, duplicate_calls=2) for _ in range(standard_runs)]
        ),
        "memory": run_flat(memory_events, trace_memory=True),
        "extended": summarize([run_flat(extended_events)]),
        "restart_midpoint": restart_digest(restart_events),
        "restore_distribution": restore_distribution(restore_iterations),
    }
    if args.stress_events > 0:
        result["stress"] = summarize([run_flat(args.stress_events)])

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
