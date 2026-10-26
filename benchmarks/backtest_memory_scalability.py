from __future__ import annotations

import argparse
import gc
import json
import os
import platform
import random
import re
import subprocess
import sys
import tempfile
import tracemalloc
from collections import Counter, defaultdict
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path
from time import perf_counter, process_time
from typing import Any

from modular_backtest.recovery import ReplayRun, run_with_runtime_snapshot_restart
from modular_backtest.scalability import (
    PerformanceConfig,
    ScalableBacktestEngine,
    ScalableJournal,
    peak_rss_bytes,
    rss_bytes,
)
from modular_backtest.scalability.manifest import incremental_candle_data_hash
from modular_core.audit import to_primitive
from modular_core.config import DataConfig, EngineConfig
from modular_core.engine import BacktestResult
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle, OrderRequest, OrderType, Side
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule
from modular_core.timeframes import TimeframeSpec


class NoopModule(StrategyModule):
    name = "benchmark_noop_scalable"
    manifest = ModuleManifest(name=name, version="0.1.0", api_version="1.0")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


class OrderLifecycleModule(StrategyModule):
    name = "benchmark_order_lifecycle_scalable"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index % 100 != 0 or view.position is not None or view.pending_orders:
            return ModuleDecision()
        price = view.current.close
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    stop_loss=price - 5.0,
                    take_profit=price + 1.0,
                    client_order_id=f"benchmark-entry-{view.index}",
                ),
            )
        )


class GCProfiler:
    def __init__(self) -> None:
        self.collections: Counter[int] = Counter()
        self.elapsed_by_generation: defaultdict[int, float] = defaultdict(float)
        self._started: dict[int, float] = {}

    def callback(self, phase: str, info: dict[str, Any]) -> None:
        generation = int(info.get("generation", -1))
        if phase == "start":
            self._started[generation] = perf_counter()
            return
        started = self._started.pop(generation, None)
        if started is not None:
            self.elapsed_by_generation[generation] += perf_counter() - started
        self.collections[generation] += 1

    def to_dict(self) -> dict[str, object]:
        return {
            "collections": {
                str(generation): self.collections[generation] for generation in range(3)
            },
            "elapsed_seconds": {
                str(generation): self.elapsed_by_generation[generation] for generation in range(3)
            },
            "total_elapsed_seconds": sum(self.elapsed_by_generation.values()),
        }


def build_bars(count: int) -> list[Candle]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    bars: list[Candle] = []
    price = 10_000.0
    for index in range(count):
        close = price + ((index % 11) - 5) * 0.1
        bars.append(
            Candle(
                timestamp=start + timedelta(minutes=5 * index),
                open=price,
                high=max(price, close) + 1.0,
                low=min(price, close) - 1.0,
                close=close,
                volume=100.0 + index % 7,
                symbol="BTCUSDT",
                timeframe="5m",
            )
        )
        price = close
    return bars


def _timeframes(max_history: int | None) -> tuple[TimeframeSpec, ...]:
    return (
        TimeframeSpec(
            3,
            "15m",
            align_to_clock=True,
            alignment_timezone="UTC",
            max_history=max_history,
        ),
        TimeframeSpec(
            12,
            "1h",
            align_to_clock=True,
            alignment_timezone="UTC",
            max_history=max_history,
        ),
        TimeframeSpec(
            48,
            "4h",
            align_to_clock=True,
            alignment_timezone="UTC",
            max_history=max_history,
        ),
        TimeframeSpec(
            288,
            "1d",
            align_to_clock=True,
            alignment_timezone="UTC",
            max_history=max_history,
        ),
    )


def _config(max_base_history: int | None) -> EngineConfig:
    return EngineConfig(
        close_open_position_at_end=True,
        data=DataConfig(
            expected_interval_seconds=300,
            gap_policy="reject",
            max_base_history=max_base_history,
        ),
    )


def _module(scenario: str) -> StrategyModule:
    if scenario == "noop_module":
        return NoopModule()
    if scenario == "order_lifecycle":
        return OrderLifecycleModule()
    raise ValueError(f"unsupported benchmark scenario: {scenario}")


def _scalable_engine(performance: PerformanceConfig, scenario: str) -> ScalableBacktestEngine:
    return ScalableBacktestEngine(
        _module(scenario),
        _config(performance.max_base_history),
        timeframes=_timeframes(performance.max_timeframe_history),
        performance=performance,
    )


class _SyntheticPreparedSource:
    def __init__(self, bars: list[Candle]) -> None:
        self._bars = bars
        self.total_bars = len(bars)
        self.first_candle = bars[0]
        self.last_candle = bars[-1]
        self.data_hash = incremental_candle_data_hash(bars)
        self.traversals = 0

    def iter_candles(self) -> Iterator[Candle]:
        self.traversals += 1
        return iter(self._bars)

    def iter_candles_from(self, start_index: int) -> Iterator[Candle]:
        self.traversals += 1
        return islice(self._bars, start_index, None)


def _commit_sha() -> str:
    configured = os.environ.get("GITHUB_SHA")
    if configured:
        return configured
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _physical_footprint_bytes() -> int | None:
    if sys.platform != "darwin":
        return None
    try:
        completed = subprocess.run(
            ["vmmap", "-summary", str(os.getpid())],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.search(
        r"Physical footprint:\s*([0-9]+(?:\.[0-9]+)?)\s*([KMG])",
        completed.stdout,
    )
    if match is None:
        return None
    multipliers = {"K": 1024, "M": 1024**2, "G": 1024**3}
    return int(float(match.group(1)) * multipliers[match.group(2)])


def _artifact_sizes(result: BacktestResult) -> tuple[int, dict[str, int]]:
    with tempfile.TemporaryDirectory(prefix="backtest-benchmark-artifacts-") as directory:
        output = result.export_directory(Path(directory))
        sizes = {
            str(path.relative_to(output)): path.stat().st_size
            for path in output.rglob("*")
            if path.is_file()
        }
        return sum(sizes.values()), dict(sorted(sizes.items()))


def _container_sizes(journal: object) -> dict[str, int]:
    names = (
        "setups",
        "setup_records",
        "features",
        "signals",
        "skips",
        "diagnostics",
        "orders",
        "fills",
        "trades",
        "equity",
        "errors",
        "_last_event_by_entity",
        "_event_ids",
    )
    return {name: sys.getsizeof(getattr(journal, name)) for name in names if hasattr(journal, name)}


def run(
    count: int,
    *,
    scenario: str,
    mode: str,
    checkpoint: str,
    measure_tracemalloc: bool,
    export_artifacts: bool,
    max_memory_mb: int | None,
) -> dict[str, object]:
    bars = build_bars(count)
    performance = PerformanceConfig(
        mode=mode,
        max_memory_mb=max_memory_mb,
        progress_interval_bars=max(count + 1, 10_000),
    )
    previous_random_state = random.getstate()
    profiler = GCProfiler()
    gc.callbacks.append(profiler.callback)
    if measure_tracemalloc:
        tracemalloc.start()
    wall_started = perf_counter()
    cpu_started = process_time()
    result: BacktestResult | None = None
    replay: ReplayRun | None = None
    prepared_source: _SyntheticPreparedSource | None = None
    try:
        random.seed(0)
        if checkpoint == "audit":
            prepared_source = _SyntheticPreparedSource(bars)
            replay = run_with_runtime_snapshot_restart(
                lambda: _scalable_engine(performance, scenario),
                prepared_source,
                restart_at_index=max(0, count // 2 - 1),
                seed=0,
            )
            result = replay.result
        else:
            result = _scalable_engine(performance, scenario).run(bars)
        elapsed = perf_counter() - wall_started
        cpu_seconds = process_time() - cpu_started
        simulation_peak_rss = peak_rss_bytes()
        traced_current: int | None = None
        traced_peak: int | None = None
        traced_allocations: int | None = None
        if measure_tracemalloc:
            traced_current, traced_peak = tracemalloc.get_traced_memory()
            snapshot = tracemalloc.take_snapshot()
            traced_allocations = sum(
                statistic.count for statistic in snapshot.statistics("filename")
            )
        journal = result.journal
        if isinstance(journal, ScalableJournal):
            events = journal.event_count
            retained_events = len(journal.events)
            skip_ranges = journal.skip_range_count
            equity_points = len(journal.equity)
            retained_equity_points = len(journal.retained_equity)
            event_bytes = journal.streaming_store.bytes_written
        else:
            events = len(journal.events)
            retained_events = events
            skip_ranges = len(journal.skips)
            equity_points = len(journal.equity)
            retained_equity_points = equity_points
            event_bytes = None
        retention_metrics = (
            journal.retention_metrics
            if isinstance(journal, ScalableJournal)
            else {
                "retained_events": len(journal.events),
                "current_base_candles": len(bars),
                "peak_base_candles": len(bars),
                "current_timeframe_candles": {
                    "15m": count // 3,
                    "1h": count // 12,
                    "4h": count // 48,
                    "1d": count // 288,
                },
                "peak_timeframe_candles": {
                    "15m": count // 3,
                    "1h": count // 12,
                    "4h": count // 48,
                    "1d": count // 288,
                },
                "journal_index_entries": len(journal._last_event_by_entity),
            }
        )
        checkpoint_bytes = len(
            json.dumps(
                to_primitive(result.checkpoint),
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        artifact_bytes: int | None = None
        artifact_files: dict[str, int] | None = None
        if export_artifacts:
            artifact_bytes, artifact_files = _artifact_sizes(result)
        return {
            "commit_sha": _commit_sha(),
            "python": sys.version,
            "platform": platform.platform(),
            "scenario": scenario,
            "mode": mode,
            "checkpoint": checkpoint,
            "bars": count,
            "elapsed_seconds": elapsed,
            "cpu_seconds": cpu_seconds,
            "bars_per_second": count / elapsed,
            "rss_bytes": rss_bytes(),
            "peak_rss_bytes": simulation_peak_rss,
            "physical_footprint_bytes": _physical_footprint_bytes(),
            "python_traced_current_bytes": traced_current,
            "python_traced_peak_bytes": traced_peak,
            "python_traced_allocations": traced_allocations,
            "gc": profiler.to_dict(),
            "candle_objects": len(bars),
            "htf_candle_counts": {
                "15m": count // 3,
                "1h": count // 12,
                "4h": count // 48,
                "1d": count // 288,
            },
            "events": events,
            "retained_events": retained_events,
            "event_stream_bytes": event_bytes,
            "skip_ranges": skip_ranges,
            "diagnostic_records": len(journal.diagnostics),
            "feature_records_retained": len(journal.features),
            "setup_records_retained": len(journal.setups),
            "signals_retained": len(journal.signals),
            "equity_points": equity_points,
            "retained_equity_points": retained_equity_points,
            "orders": len(journal.orders),
            "fills": len(journal.fills),
            "trades": len(journal.trades),
            "container_shallow_bytes": _container_sizes(journal),
            "retention": retention_metrics,
            "checkpoint_bytes": checkpoint_bytes,
            "recovery_mode": replay.recovery_mode if replay is not None else "disabled",
            "runtime_snapshot_bytes": (
                replay.checkpoint.snapshot_bytes
                if replay is not None and replay.checkpoint is not None
                else None
            ),
            "snapshot_event_stream_bytes": (
                replay.checkpoint.event_stream_bytes
                if replay is not None and replay.checkpoint is not None
                else None
            ),
            "restart_verified": replay.restart_verified if replay is not None else False,
            "simulation_passes": replay.simulation_passes if replay is not None else 1,
            "source_traversals": replay.source_traversals if replay is not None else 1,
            "source_rows_replayed": replay.source_rows_replayed if replay is not None else 0,
            "observed_source_traversals": (
                prepared_source.traversals if prepared_source is not None else 1
            ),
            "artifact_bytes": artifact_bytes,
            "artifact_files": artifact_files,
        }
    finally:
        try:
            gc.callbacks.remove(profiler.callback)
        except ValueError:  # pragma: no cover - defensive against external mutation
            pass
        if measure_tracemalloc:
            tracemalloc.stop()
        random.setstate(previous_random_state)
        if result is not None and isinstance(result.journal, ScalableJournal):
            result.journal.streaming_store.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, required=True)
    parser.add_argument(
        "--scenario",
        choices=("noop_module", "order_lifecycle"),
        default="noop_module",
    )
    parser.add_argument(
        "--mode",
        choices=("canonical_audit", "research_fast"),
        default="research_fast",
    )
    parser.add_argument("--checkpoint", choices=("disabled", "audit"), default="disabled")
    parser.add_argument("--memory", action="store_true", help="enable tracemalloc")
    parser.add_argument(
        "--export-artifacts",
        action="store_true",
        help="measure output sizes after the timed simulation",
    )
    parser.add_argument("--max-memory-mb", type=int, default=9216)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.bars <= 0:
        parser.error("--bars must be positive")
    if args.max_memory_mb < 0:
        parser.error("--max-memory-mb must be non-negative")
    payload = run(
        args.bars,
        scenario=args.scenario,
        mode=args.mode,
        checkpoint=args.checkpoint,
        measure_tracemalloc=args.memory,
        export_artifacts=args.export_artifacts,
        max_memory_mb=args.max_memory_mb or None,
    )
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output is not None:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
