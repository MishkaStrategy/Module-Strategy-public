"""Reproducible performance and restart benchmark for Breakout–Return."""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from time import perf_counter
from typing import Any

from modular_core import Candle, MarketView
from strategy_modules.breakout_return import BreakoutReturnModule

START = datetime(2020, 1, 1, tzinfo=timezone.utc)


@dataclass(frozen=True, slots=True)
class RunResult:
    seconds: float
    peak_memory_bytes: int | None
    checkpoint_bytes: int
    restore_seconds_median: float
    final_state_sha256: str
    semantic_state_sha256: str


def build_bar(index: int) -> Candle:
    """Create deterministic, non-breakout 5m input without retaining history."""
    base = 100.0 + ((index % 101) - 50) * 0.0001
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=base,
        high=base + 0.5 + (index % 7) * 0.001,
        low=base - 0.5 - (index % 11) * 0.001,
        close=base,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def build_module(lookback: int) -> BreakoutReturnModule:
    return BreakoutReturnModule(
        lookback_bars=lookback,
        direction="above",
        breakout_buffer_bps=5_000,
    )


def json_state(module: BreakoutReturnModule) -> dict[str, Any]:
    return json.loads(json.dumps(module.snapshot_state(), sort_keys=True))


def restore_latency(
    state: dict[str, Any],
    lookback: int,
    samples: int,
) -> float:
    measurements: list[float] = []
    for _ in range(samples):
        restored = build_module(lookback)
        started = perf_counter()
        restored.restore_state(state)
        measurements.append(perf_counter() - started)
    return statistics.median(measurements)


def run_workload(
    *,
    events: int,
    lookback: int,
    restart_at: int | None,
    measure_memory: bool,
    restore_samples: int,
) -> RunResult:
    module = build_module(lookback)
    if measure_memory:
        tracemalloc.start()

    started = perf_counter()
    for index in range(events):
        bar = build_bar(index)
        module.on_bar(
            MarketView(
                base_bars=(bar,),
                timeframe_bars={},
                index=index,
                cash=10_000.0,
                equity=10_000.0,
                position=None,
                pending_orders=(),
            )
        )
        if restart_at is not None and index + 1 == restart_at:
            state = json_state(module)
            restored = build_module(lookback)
            restored.restore_state(state)
            module = restored
    seconds = perf_counter() - started

    peak_memory = tracemalloc.get_traced_memory()[1] if measure_memory else None
    if measure_memory:
        tracemalloc.stop()

    state = json_state(module)
    canonical_state = json.dumps(
        state,
        sort_keys=True,
        separators=(",", ":"),
    )
    semantic_state = dict(state)
    semantic_state.pop("counters", None)
    canonical_semantic_state = json.dumps(
        semantic_state,
        sort_keys=True,
        separators=(",", ":"),
    )
    return RunResult(
        seconds=seconds,
        peak_memory_bytes=peak_memory,
        checkpoint_bytes=len(canonical_state.encode("utf-8")),
        restore_seconds_median=restore_latency(
            state,
            lookback,
            restore_samples,
        ),
        final_state_sha256=hashlib.sha256(canonical_state.encode("utf-8")).hexdigest(),
        semantic_state_sha256=hashlib.sha256(canonical_semantic_state.encode("utf-8")).hexdigest(),
    )


def summarize(
    args: argparse.Namespace,
    rows: list[RunResult],
) -> dict[str, Any]:
    times = [row.seconds for row in rows]
    peaks = [row.peak_memory_bytes for row in rows if row.peak_memory_bytes is not None]
    state_hashes = {row.final_state_sha256 for row in rows}
    semantic_hashes = {row.semantic_state_sha256 for row in rows}
    if len(state_hashes) != 1:
        raise RuntimeError("benchmark runs produced different final states")
    if len(semantic_hashes) != 1:
        raise RuntimeError("benchmark runs produced different semantic states")
    median_seconds = statistics.median(times)
    return {
        "benchmark": "breakout_return_deep",
        "python": sys.version.split()[0],
        "events": args.events,
        "timeframe": "5m",
        "lookback": args.lookback,
        "runs": args.runs,
        "restart_at": args.restart_at,
        "restore_samples_per_run": args.restore_samples,
        "seconds_min": min(times),
        "seconds_median": median_seconds,
        "seconds_max": max(times),
        "throughput_median_events_per_second": (args.events / median_seconds),
        "peak_memory_bytes_median": (statistics.median(peaks) if peaks else None),
        "checkpoint_bytes_median": statistics.median(row.checkpoint_bytes for row in rows),
        "restore_seconds_median": statistics.median(row.restore_seconds_median for row in rows),
        "final_state_sha256": next(iter(state_hashes)),
        "semantic_state_sha256": next(iter(semantic_hashes)),
        "results": [asdict(row) for row in rows],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, required=True)
    parser.add_argument("--lookback", type=int, default=1_000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--restart-at", type=int)
    parser.add_argument("--restore-samples", type=int, default=100)
    parser.add_argument("--memory", action="store_true")
    args = parser.parse_args()
    if args.events < 1 or args.runs < 1 or args.restore_samples < 1:
        raise SystemExit("events, runs and restore-samples must be positive")
    if args.restart_at is not None and not 1 <= args.restart_at < args.events:
        raise SystemExit("restart-at must be inside the workload")

    rows = [
        run_workload(
            events=args.events,
            lookback=args.lookback,
            restart_at=args.restart_at,
            measure_memory=args.memory,
            restore_samples=args.restore_samples,
        )
        for _ in range(args.runs)
    ]
    print(
        json.dumps(
            summarize(args, rows),
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
