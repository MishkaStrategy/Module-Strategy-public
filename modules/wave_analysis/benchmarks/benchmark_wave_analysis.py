from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import time
import tracemalloc
from datetime import UTC, datetime, timedelta

from modular_core import Candle, MarketView

from wave_analysis import WaveAnalysisModule


def make_module() -> WaveAnalysisModule:
    return WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.01",
        maximum_retained_bars=512,
        maximum_retained_pivots=128,
        maximum_retained_waves=127,
        maximum_retained_atr_values=64,
        elliott_enabled=True,
        maximum_hypotheses=3,
        publish_provisional=False,
        publish_diagnostics=False,
    )


def make_candle(index: int, start: datetime) -> Candle:
    cycle = index % 40
    price = 100.0 + (cycle if cycle < 20 else 40 - cycle) * 0.5
    return Candle(
        timestamp=start + timedelta(minutes=index),
        open=price - 0.05,
        high=price + 0.2,
        low=price - 0.2,
        close=price,
        volume=1000.0 + (index % 17) * 10.0,
        symbol="BTCUSDT",
        timeframe="1m",
    )


def run_once(bars: int, *, measure_memory: bool = False, restart_at: int | None = None) -> dict[str, object]:
    module = make_module()
    start = datetime(2020, 1, 1, tzinfo=UTC)
    if measure_memory:
        tracemalloc.start()
    begin = time.perf_counter()
    published_features = 0
    for index in range(bars):
        candle = make_candle(index, start)
        decision = module.on_bar(
            MarketView(
                base_bars=(candle,),
                timeframe_bars={"1m": (candle,)},
                index=index,
                cash=0.0,
                equity=0.0,
                position=None,
                pending_orders=(),
            )
        )
        published_features += len(decision.features)
        if restart_at is not None and index + 1 == restart_at:
            snapshot = module.snapshot_state()
            restored = make_module()
            restored.restore_state(snapshot)
            module = restored
    elapsed = time.perf_counter() - begin
    peak = 0
    if measure_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    snapshot = module.snapshot_state()
    checkpoint = json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
    restore_start = time.perf_counter()
    restored = make_module()
    restored.restore_state(snapshot)
    restore_latency = time.perf_counter() - restore_start
    state = snapshot["timeframes"]["1m"]
    return {
        "bars": bars,
        "pivots": len(state["confirmed_pivots"]),
        "wave_legs": len(state["wave_legs"]),
        "hypotheses": len(state["hypotheses"]),
        "published_features": published_features,
        "elapsed_seconds": elapsed,
        "throughput_bars_per_second": bars / elapsed,
        "peak_memory_bytes": peak,
        "checkpoint_size_bytes": len(checkpoint),
        "restore_latency_seconds": restore_latency,
        "result_digest": hashlib.sha256(checkpoint).hexdigest(),
        "state_digest": module.state_digest,
        "restore_digest_equal": restored.state_digest == module.state_digest,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, default=5_000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--memory-bars", type=int, default=1_000)
    args = parser.parse_args()
    if min(args.bars, args.runs, args.memory_bars) <= 0:
        raise SystemExit("bars, runs and memory-bars must be positive")
    results = [run_once(args.bars, restart_at=args.bars // 2) for _ in range(args.runs)]
    memory = run_once(min(args.bars, args.memory_bars), measure_memory=True)
    elapsed = [float(item["elapsed_seconds"]) for item in results]
    throughput = [float(item["throughput_bars_per_second"]) for item in results]
    report = {
        "bars": args.bars,
        "runs": args.runs,
        "elapsed_min": min(elapsed),
        "elapsed_median": statistics.median(elapsed),
        "elapsed_max": max(elapsed),
        "throughput_min": min(throughput),
        "throughput_median": statistics.median(throughput),
        "throughput_max": max(throughput),
        "checkpoint_size_max_bytes": max(int(item["checkpoint_size_bytes"]) for item in results),
        "restore_latency_max_seconds": max(float(item["restore_latency_seconds"]) for item in results),
        "digests_equal_across_runs": len({item["state_digest"] for item in results}) == 1,
        "restore_digest_equal_all_runs": all(bool(item["restore_digest_equal"]) for item in results),
        "memory_sample": memory,
        "sample": results[0],
    }
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
