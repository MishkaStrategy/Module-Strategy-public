from __future__ import annotations

import argparse
import hashlib
import json
import platform
import tracemalloc
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from statistics import median
from time import perf_counter

from modular_core import Candle, MarketView
from market_regime import MarketRegimeModule


@dataclass(frozen=True, slots=True)
class Sample:
    elapsed_seconds: float
    throughput_events_per_second: float
    peak_traced_bytes: int | None
    checkpoint_bytes: int
    restore_seconds: float
    publications: int
    feature_count: int
    output_digest: str
    state_digest: str


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    python_version: str
    events: int
    runs: int
    timeframe: str
    trace_memory: bool
    restart_at: int | None
    samples: tuple[Sample, ...]
    median_seconds: float
    minimum_seconds: float
    maximum_seconds: float
    median_throughput: float
    maximum_peak_traced_bytes: int | None
    maximum_checkpoint_bytes: int
    median_restore_seconds: float
    deterministic_outputs: bool
    deterministic_states: bool


def target_bar(index: int) -> Candle:
    start = datetime(2020, 1, 1, tzinfo=timezone.utc)
    close = 10_000.0 + index * 0.25 + (index % 17) * 0.03
    return Candle(
        timestamp=start + timedelta(hours=index),
        open=close - 0.1,
        high=close + 1.0,
        low=close - 1.0,
        close=close,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="1h",
    )


def market_view(candle: Candle) -> MarketView:
    return MarketView(
        base_bars=(candle,),
        timeframe_bars={"1h": (candle,)},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
    )


def canonical_state(module: MarketRegimeModule) -> bytes:
    return json.dumps(module.snapshot_state(), sort_keys=True, separators=(",", ":")).encode()


def run_once(
    *,
    events: int,
    trace_memory: bool,
    restart_at: int | None,
    cross_diagnostics_restore: bool,
) -> Sample:
    module = MarketRegimeModule(publish_diagnostics=False)
    module.on_start()
    output_hash = hashlib.sha256()
    publications = 0
    feature_count = 0
    restore_seconds = 0.0
    if trace_memory:
        tracemalloc.start()
    started = perf_counter()
    for index in range(events):
        decision = module.on_bar(market_view(target_bar(index)))
        if decision.features:
            publications += 1
            feature_count += len(decision.features)
            for feature in decision.features:
                output_hash.update((feature.feature_id or "").encode())
                output_hash.update(repr(feature.value).encode())
        if restart_at is not None and index + 1 == restart_at:
            checkpoint = module.snapshot_state()
            restored = MarketRegimeModule(publish_diagnostics=False)
            restore_started = perf_counter()
            restored.restore_state(checkpoint)
            restore_seconds += perf_counter() - restore_started
            module = restored
    elapsed = perf_counter() - started
    peak: int | None = None
    if trace_memory:
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
    encoded = canonical_state(module)
    verifier = MarketRegimeModule(publish_diagnostics=cross_diagnostics_restore)
    restore_started = perf_counter()
    verifier.restore_state(module.snapshot_state())
    restore_seconds += perf_counter() - restore_started
    if canonical_state(verifier) != encoded:
        raise RuntimeError("checkpoint verification changed canonical state")
    return Sample(
        elapsed_seconds=elapsed,
        throughput_events_per_second=events / elapsed,
        peak_traced_bytes=peak,
        checkpoint_bytes=len(encoded),
        restore_seconds=restore_seconds,
        publications=publications,
        feature_count=feature_count,
        output_digest=output_hash.hexdigest(),
        state_digest=hashlib.sha256(encoded).hexdigest(),
    )


def build_report(
    *,
    events: int,
    runs: int,
    trace_memory: bool,
    restart_at: int | None,
    cross_diagnostics_restore: bool,
) -> BenchmarkReport:
    samples = tuple(
        run_once(
            events=events,
            trace_memory=trace_memory,
            restart_at=restart_at,
            cross_diagnostics_restore=cross_diagnostics_restore,
        )
        for _ in range(runs)
    )
    elapsed = [sample.elapsed_seconds for sample in samples]
    throughput = [sample.throughput_events_per_second for sample in samples]
    restore = [sample.restore_seconds for sample in samples]
    peaks = [sample.peak_traced_bytes for sample in samples if sample.peak_traced_bytes is not None]
    return BenchmarkReport(
        python_version=platform.python_version(),
        events=events,
        runs=runs,
        timeframe="1h",
        trace_memory=trace_memory,
        restart_at=restart_at,
        samples=samples,
        median_seconds=median(elapsed),
        minimum_seconds=min(elapsed),
        maximum_seconds=max(elapsed),
        median_throughput=median(throughput),
        maximum_peak_traced_bytes=max(peaks) if peaks else None,
        maximum_checkpoint_bytes=max(sample.checkpoint_bytes for sample in samples),
        median_restore_seconds=median(restore),
        deterministic_outputs=len({sample.output_digest for sample in samples}) == 1,
        deterministic_states=len({sample.state_digest for sample in samples}) == 1,
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--events", type=int, default=20_000)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--trace-memory", action="store_true")
    parser.add_argument("--restart-at", type=int)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--same-diagnostics-restore", action="store_true")
    args = parser.parse_args()
    if args.events < 1 or args.runs < 1:
        parser.error("events and runs must be positive")
    if args.restart_at is not None and not 1 <= args.restart_at < args.events:
        parser.error("restart-at must be in [1, events)")
    report = build_report(
        events=args.events,
        runs=args.runs,
        trace_memory=args.trace_memory,
        restart_at=args.restart_at,
        cross_diagnostics_restore=not args.same_diagnostics_restore,
    )
    payload = json.dumps(asdict(report), indent=2, sort_keys=True)
    print(payload)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
