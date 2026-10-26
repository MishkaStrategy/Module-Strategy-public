from __future__ import annotations

import argparse
import cProfile
import hashlib
import json
import platform
import resource
import statistics
import sys
import time
import tracemalloc
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from modular_core import Candle, Feature, FeatureView, MarketView

from modular_modules.entry_model import EntryModelModule

START = datetime(2020, 1, 1, tzinfo=UTC)
SYMBOL = "BTCUSDT"
TIMEFRAME = "5m"


def make_view(index: int, *, with_feature: bool) -> MarketView:
    timestamp = START + timedelta(minutes=5 * index)
    bar = Candle(timestamp, 100, 101, 99, 100, 1, SYMBOL, TIMEFRAME)
    features: tuple[Feature, ...] = ()
    if with_feature:
        features = (
            Feature(
                key="setup.confirmed",
                source_module="benchmark",
                created_at=bar.close_time,
                value={
                    "setup_id": f"setup-{index}",
                    "status": "confirmed",
                    "side": "buy" if index % 2 == 0 else "sell",
                    "confirmed_at": bar.close_time.isoformat(),
                    "confirmation_price": "100",
                    "extreme_price": "90" if index % 2 == 0 else "110",
                },
                symbol=SYMBOL,
                timeframe=TIMEFRAME,
                valid_until=bar.close_time,
                schema_version="1",
                feature_id=f"source-{index}",
            ),
        )
    return MarketView(
        base_bars=(bar,),
        timeframe_bars={},
        index=index,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        features=FeatureView(features, bar.close_time),
    )


def run_workload(
    events: int, *, with_feature: bool, restart_at: int | None = None
) -> dict[str, object]:
    module = EntryModelModule(max_processed_setups=10_000)
    digest = hashlib.sha256()
    plans = 0
    started = time.perf_counter()
    for index in range(events):
        decision = module.on_bar(make_view(index, with_feature=with_feature))
        for feature in decision.features:
            if feature.key == "entry_model.plan":
                plans += 1
                digest.update(str(feature.feature_id).encode("utf-8"))
        if restart_at is not None and index + 1 == restart_at:
            state = module.snapshot_state()
            replacement = EntryModelModule(max_processed_setups=10_000)
            replacement.restore_state(state)
            module = replacement
    elapsed = time.perf_counter() - started
    snapshot = module.snapshot_state()
    checkpoint_json = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    return {
        "events": events,
        "with_feature": with_feature,
        "restart_at": restart_at,
        "seconds": elapsed,
        "throughput_per_second": events / elapsed,
        "plans": plans,
        "digest": digest.hexdigest(),
        "processed_state": len(snapshot["processed"]),
        "seen_feature_state": len(snapshot["seen_features"]),
        "checkpoint_bytes": len(checkpoint_json.encode("utf-8")),
    }


def measure(
    label: str,
    runs: int,
    callback: Callable[[], dict[str, object]],
) -> dict[str, object]:
    rows = [callback() for _ in range(runs)]
    seconds = [float(row["seconds"]) for row in rows]
    throughput = [float(row["throughput_per_second"]) for row in rows]
    digests = {str(row["digest"]) for row in rows}
    if len(digests) != 1:
        raise RuntimeError(f"non-deterministic digest in {label}")
    return {
        "label": label,
        "runs": runs,
        "events": rows[0]["events"],
        "with_feature": rows[0]["with_feature"],
        "restart_at": rows[0]["restart_at"],
        "seconds": {
            "minimum": min(seconds),
            "median": statistics.median(seconds),
            "maximum": max(seconds),
        },
        "throughput_per_second": {
            "minimum": min(throughput),
            "median": statistics.median(throughput),
            "maximum": max(throughput),
        },
        "plans": rows[0]["plans"],
        "digest": rows[0]["digest"],
        "processed_state": rows[0]["processed_state"],
        "seen_feature_state": rows[0]["seen_feature_state"],
        "checkpoint_bytes": rows[0]["checkpoint_bytes"],
    }


def restore_measurements(snapshot: dict[str, object], runs: int = 20) -> dict[str, float | int]:
    samples: list[float] = []
    for _ in range(runs):
        module = EntryModelModule(max_processed_setups=10_000)
        started = time.perf_counter()
        module.restore_state(snapshot)
        samples.append(time.perf_counter() - started)
    return {
        "runs": runs,
        "minimum_seconds": min(samples),
        "median_seconds": statistics.median(samples),
        "maximum_seconds": max(samples),
    }


def normalize_max_rss_kib(raw_value: int, *, system_name: str | None = None) -> int:
    if raw_value < 0:
        raise ValueError("max RSS cannot be negative")
    resolved_system = platform.system() if system_name is None else system_name
    if resolved_system == "Darwin":
        return (raw_value + 1023) // 1024
    return raw_value


def memory_measurement(events: int = 20_000) -> dict[str, int]:
    tracemalloc.start()
    result = run_workload(events, with_feature=True)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "events": events,
        "current_bytes": current,
        "peak_bytes": peak,
        "checkpoint_bytes": int(result["checkpoint_bytes"]),
        "max_rss_kib": normalize_max_rss_kib(
            int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--profile-output", type=Path, required=True)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args()

    if args.quick:
        workloads = [
            ("smoke-empty", 5, lambda: run_workload(1_000, with_feature=False)),
            ("smoke-plans", 5, lambda: run_workload(1_000, with_feature=True)),
            (
                "restart-plans",
                5,
                lambda: run_workload(2_000, with_feature=True, restart_at=1_000),
            ),
        ]
    else:
        workloads = [
            ("smoke-empty", 5, lambda: run_workload(1_000, with_feature=False)),
            ("smoke-plans", 5, lambda: run_workload(1_000, with_feature=True)),
            ("standard-plans", 5, lambda: run_workload(50_000, with_feature=True)),
            ("extended-plans", 3, lambda: run_workload(250_000, with_feature=True)),
            ("stress-empty", 3, lambda: run_workload(1_000_000, with_feature=False)),
            (
                "restart-standard",
                3,
                lambda: run_workload(50_000, with_feature=True, restart_at=25_000),
            ),
        ]

    results = [measure(label, runs, callback) for label, runs, callback in workloads]
    populated = EntryModelModule(max_processed_setups=10_000)
    for index in range(20_000):
        populated.on_bar(make_view(index, with_feature=True))
    snapshot = dict(populated.snapshot_state())

    profile = cProfile.Profile()
    profile.enable()
    run_workload(5_000, with_feature=True)
    profile.disable()
    args.profile_output.parent.mkdir(parents=True, exist_ok=True)
    profile.dump_stats(args.profile_output)

    payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "python": sys.version,
        "platform": platform.platform(),
        "module_version": "0.2.0",
        "workloads": results,
        "memory": memory_measurement(),
        "restore": restore_measurements(snapshot),
        "notes": [
            "All bars and features are generated deterministically in memory.",
            "No network, disk market-data loading, or system clock is used by the module.",
            "Extended workloads use three runs because they are intentionally long; short workloads use five.",
            "max_rss_kib is normalized across platforms: Darwin ru_maxrss bytes are converted to KiB; other supported Unix values are treated as KiB.",
        ],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    print(json.dumps(payload, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
