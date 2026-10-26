from __future__ import annotations

import argparse
import json
import tracemalloc
from datetime import UTC, datetime, timedelta
from time import perf_counter

from modular_backtest.recovery import run_with_replay_restart
from modular_core.config import DataConfig, EngineConfig
from modular_core.engine import BacktestEngine
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule
from modular_core.timeframes import TimeframeSpec


class NoopModule(StrategyModule):
    name = "benchmark_noop"
    manifest = ModuleManifest(name=name, version="0.1.0", api_version="1.0")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


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


def _engine() -> BacktestEngine:
    return BacktestEngine(
        NoopModule(),
        EngineConfig(
            close_open_position_at_end=True,
            data=DataConfig(expected_interval_seconds=300, gap_policy="reject"),
        ),
        timeframes=(
            TimeframeSpec(3, "15m", align_to_clock=True, alignment_timezone="UTC"),
            TimeframeSpec(12, "1h", align_to_clock=True, alignment_timezone="UTC"),
            TimeframeSpec(48, "4h", align_to_clock=True, alignment_timezone="UTC"),
            TimeframeSpec(288, "1d", align_to_clock=True, alignment_timezone="UTC"),
        ),
    )


def run(count: int, *, restart_at_index: int | None = None) -> dict[str, object]:
    bars = build_bars(count)
    tracemalloc.start()
    started = perf_counter()
    replay = run_with_replay_restart(
        _engine,
        bars,
        restart_at_index=restart_at_index,
        seed=0,
    )
    elapsed = perf_counter() - started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = replay.result
    checkpoint = replay.checkpoint
    return {
        "run_id": result.run_manifest.run_id,
        "data_hash": result.run_manifest.data_hash,
        "config_hash": result.run_manifest.config_hash,
        "module_hash": result.run_manifest.module_hash,
        "bars_processed": result.bars_processed,
        "events": len(result.journal.events),
        "trades": len(result.journal.trades),
        "modules": list(result.modules),
        "restart_at_index": restart_at_index,
        "restart_verified": replay.restart_verified,
        "continuous_sha256": replay.continuous_sha256,
        "restarted_sha256": replay.restarted_sha256,
        "checkpoint_runtime_sha256": (
            checkpoint.runtime_sha256 if checkpoint is not None else None
        ),
        "checkpoint_event_sha256": checkpoint.event_sha256 if checkpoint is not None else None,
        "elapsed_seconds": elapsed,
        "events_per_second": count / elapsed,
        "peak_memory_bytes": peak,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bars", type=int, required=True)
    parser.add_argument(
        "--restart-at",
        type=int,
        help="zero-based checkpoint bar; defaults to midpoint when --with-restart is set",
    )
    parser.add_argument("--with-restart", action="store_true")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.bars <= 0:
        parser.error("--bars must be positive")
    restart_at = args.restart_at
    if args.with_restart and restart_at is None:
        restart_at = max(0, args.bars // 2 - 1)
    payload = run(args.bars, restart_at_index=restart_at)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            handle.write(rendered + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
