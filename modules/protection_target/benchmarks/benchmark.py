from __future__ import annotations

import argparse
import json
import platform
import resource
import statistics
import time
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from modular_core import Candle, Feature, FeatureView, MarketView

from modular_strategy_protection_target import ProtectionTargetModule, __version__

START = datetime(2026, 1, 1, tzinfo=UTC)


def make_view(index: int) -> MarketView:
    timestamp = START + timedelta(minutes=5 * index)
    bar = Candle(
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    close_time = bar.close_time
    side = "buy" if index % 2 == 0 else "sell"
    entry = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=close_time,
        value={
            "schema_version": "1",
            "entry_plan_id": f"entry-{index}",
            "setup_id": f"setup-{index}",
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "side": side,
            "entry_price": "100",
            "invalidation_price": "99" if side == "buy" else "101",
            "created_at": close_time.isoformat(),
        },
        symbol="BTCUSDT",
        timeframe="5m",
        valid_until=close_time,
        feature_id=f"entry-{index}:feature",
    )
    return MarketView(
        base_bars=(bar,),
        timeframe_bars={},
        index=index,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        features=FeatureView((entry,), close_time),
    )


def run_segment(module: ProtectionTargetModule, start: int, count: int) -> float:
    begin = time.perf_counter()
    for index in range(start, start + count):
        decision = module.on_bar(make_view(index))
        if len(decision.features) != 1:
            raise RuntimeError(f"unexpected decision at {index}: {decision}")
    return time.perf_counter() - begin


def short_benchmark(plans: int, repeat: int) -> dict[str, Any]:
    samples: list[float] = []
    peaks: list[int] = []
    checkpoints: list[int] = []
    restores: list[float] = []
    for _ in range(repeat):
        module = ProtectionTargetModule(max_processed_plans=max(plans, 1))
        tracemalloc.start()
        elapsed = run_segment(module, 0, plans)
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        state = module.snapshot_state()
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        restore_start = time.perf_counter()
        restored = ProtectionTargetModule(max_processed_plans=max(plans, 1))
        restored.restore_state(state)
        restore_elapsed = time.perf_counter() - restore_start
        assert restored.snapshot_state() == state
        samples.append(elapsed)
        peaks.append(peak)
        checkpoints.append(len(encoded))
        restores.append(restore_elapsed)
    median = statistics.median(samples)
    return {
        "kind": "short_traced",
        "module_version": __version__,
        "python": platform.python_version(),
        "plans": plans,
        "repeat": repeat,
        "samples_seconds": samples,
        "minimum_seconds": min(samples),
        "median_seconds": median,
        "maximum_seconds": max(samples),
        "plans_per_second": plans / median,
        "peak_mib": max(peaks) / (1024 * 1024),
        "checkpoint_bytes_median": int(statistics.median(checkpoints)),
        "restore_ms_median": statistics.median(restores) * 1000,
    }


def stress_benchmark(total: int, segment: int, max_state: int) -> dict[str, Any]:
    module = ProtectionTargetModule(max_processed_plans=max_state)
    segments: list[dict[str, Any]] = []
    start = 0
    total_started = time.perf_counter()
    while start < total:
        count = min(segment, total - start)
        rss_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        elapsed = run_segment(module, start, count)
        state = module.snapshot_state()
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":")).encode()
        restore_started = time.perf_counter()
        restored = ProtectionTargetModule(max_processed_plans=max_state)
        restored.restore_state(state)
        restore_elapsed = time.perf_counter() - restore_started
        module = restored
        rss_after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        segments.append(
            {
                "start": start,
                "events": count,
                "seconds": elapsed,
                "events_per_second": count / elapsed,
                "checkpoint_bytes": len(encoded),
                "restore_ms": restore_elapsed * 1000,
                "processed_state": len(state["processed"]),
                "rss_delta_mib": max(0, rss_after - rss_before) / 1024,
            }
        )
        start += count
    total_elapsed = time.perf_counter() - total_started
    digest = module.snapshot_state()["state_digest"]
    return {
        "kind": "restart_stress",
        "module_version": __version__,
        "python": platform.python_version(),
        "events": total,
        "segment_events": segment,
        "max_processed_plans": max_state,
        "total_seconds": total_elapsed,
        "aggregate_events_per_second": total / total_elapsed,
        "segments": segments,
        "maximum_checkpoint_bytes": max(row["checkpoint_bytes"] for row in segments),
        "median_restore_ms": statistics.median(row["restore_ms"] for row in segments),
        "maximum_rss_delta_mib": max(row["rss_delta_mib"] for row in segments),
        "final_processed_state": len(module.snapshot_state()["processed"]),
        "final_state_digest": digest,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("short", "stress"), default="short")
    parser.add_argument("--plans", type=int, default=2_000)
    parser.add_argument("--repeat", type=int, default=5)
    parser.add_argument("--events", type=int, default=1_000_000)
    parser.add_argument("--segment", type=int, default=100_000)
    parser.add_argument("--max-state", type=int, default=4_096)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.mode == "short":
        result = short_benchmark(args.plans, args.repeat)
    else:
        result = stress_benchmark(args.events, args.segment, args.max_state)
    text = json.dumps(result, indent=2, sort_keys=True)
    print(text)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
