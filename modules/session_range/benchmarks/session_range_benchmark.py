from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from modular_core import Candle, MarketView

from modular_session_range import MODULE_VERSION, SessionRangeModule


def _process(count: int, *, trace_memory: bool = False) -> tuple[float, int, int, float]:
    module = SessionRangeModule(
        [{"name": "range", "timezone": "UTC", "start": "00:00", "end": "04:00"}]
    )
    start = datetime(2020, 1, 1, tzinfo=UTC)
    recent: list[Candle] = []
    if trace_memory:
        tracemalloc.start()
    started = time.perf_counter()
    for index in range(count):
        timestamp = start + timedelta(minutes=5 * index)
        price = 100.0 + (index % 100) * 0.01
        bar = Candle(
            timestamp=timestamp,
            open=price,
            high=price + 1.0,
            low=price - 1.0,
            close=price + 0.1,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        recent.append(bar)
        recent = recent[-2:]
        module.on_bar(
            MarketView(
                base_bars=recent,
                timeframe_bars={},
                index=index,
                cash=1.0,
                equity=1.0,
                position=None,
                pending_orders=(),
            )
        )
    elapsed = time.perf_counter() - started
    peak = 0
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    snapshot = module.snapshot_state()
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    restored = SessionRangeModule(
        [{"name": "range", "timezone": "UTC", "start": "00:00", "end": "04:00"}]
    )
    restore_started = time.perf_counter()
    restored.restore_state(json.loads(encoded))
    restore_seconds = time.perf_counter() - restore_started
    assert restored.snapshot_state() == snapshot
    return elapsed, peak, len(encoded.encode("utf-8")), restore_seconds


def benchmark(count: int, runs: int, *, memory_run: bool) -> dict[str, Any]:
    samples = [_process(count) for _ in range(runs)]
    times = [sample[0] for sample in samples]
    memory = _process(count, trace_memory=True) if memory_run else (0.0, 0, 0, 0.0)
    return {
        "events": count,
        "runs": runs,
        "minimum_seconds": min(times),
        "median_seconds": statistics.median(times),
        "maximum_seconds": max(times),
        "throughput_events_per_second": count / statistics.median(times),
        "peak_memory_bytes": memory[1],
        "checkpoint_size_bytes": max(sample[2] for sample in samples),
        "restore_median_seconds": statistics.median(sample[3] for sample in samples),
    }


def run_suite(
    workloads: tuple[tuple[int, int], ...], *, memory_events: int = 10_000
) -> list[dict[str, Any]]:
    results = [benchmark(count, runs, memory_run=False) for count, runs in workloads]
    memory = _process(memory_events, trace_memory=True)
    for result in results:
        if result["events"] == memory_events:
            result["peak_memory_bytes"] = memory[1]
            break
    else:
        raise ValueError("memory_events must identify one configured workload")
    return results


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--profile", choices=("baseline", "candidate"), default="candidate")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--events", type=int)
    parser.add_argument("--runs", type=int, default=1)
    parser.add_argument("--trace-memory", action="store_true")
    args = parser.parse_args()
    if args.events is not None:
        if args.events < 1 or args.runs < 1:
            parser.error("--events and --runs must be positive")
        workloads = [benchmark(args.events, args.runs, memory_run=args.trace_memory)]
    else:
        if args.trace_memory or args.runs != 1:
            parser.error("--runs and --trace-memory require --events")
        workloads = run_suite(((1_000, 5), (10_000, 5), (50_000, 5)))
    result = {
        "profile": args.profile,
        "module_version": MODULE_VERSION,
        "python": platform.python_version(),
        "workloads": workloads,
    }
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
