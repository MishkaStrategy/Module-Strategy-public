from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

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
        feature_snapshot_limit=32,
        elliott_enabled=True,
        maximum_hypotheses=3,
        publish_provisional=False,
        publish_diagnostics=False,
    )


def candle(index: int) -> Candle:
    cycle = index % 40
    price = 100.0 + (cycle if cycle < 20 else 40 - cycle) * 0.5
    return Candle(
        datetime(2020, 1, 1, tzinfo=UTC) + timedelta(minutes=index),
        price - 0.05,
        price + 0.2,
        price - 0.2,
        price,
        1000.0 + (index % 17) * 10.0,
        "BTCUSDT",
        "1m",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", type=int, required=True)
    parser.add_argument("--count", type=int, required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    module = make_module()
    restored = False
    if args.state.exists() and args.state.stat().st_size:
        module.restore_state(json.loads(args.state.read_text()))
        restored = True
    begin = time.perf_counter()
    feature_count = 0
    for index in range(args.start, args.start + args.count):
        item = candle(index)
        feature_count += len(module.on_bar(MarketView((item,), {"1m": (item,)}, index, 0.0, 0.0, None, ())).features)
    elapsed = time.perf_counter() - begin
    snapshot = module.snapshot_state()
    args.state.write_text(json.dumps(snapshot, sort_keys=True, separators=(",", ":")))
    tf = snapshot["timeframes"]["1m"]
    report = {
        "start": args.start,
        "count": args.count,
        "end_exclusive": args.start + args.count,
        "restored": restored,
        "elapsed_seconds": elapsed,
        "throughput_bars_per_second": args.count / elapsed,
        "features": feature_count,
        "pivots": len(tf["confirmed_pivots"]),
        "waves": len(tf["wave_legs"]),
        "hypotheses": len(tf["hypotheses"]),
        "checkpoint_size_bytes": args.state.stat().st_size,
        "state_digest": module.state_digest,
    }
    args.report.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(json.dumps(report, sort_keys=True))


if __name__ == "__main__":
    main()
