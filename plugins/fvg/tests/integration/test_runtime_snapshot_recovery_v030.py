from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_backtest.recovery import run_with_runtime_snapshot_restart
from modular_backtest.scalability import (
    PerformanceConfig,
    ScalableBacktestEngine,
    ScalableJournal,
)
from modular_backtest.scalability.manifest import incremental_candle_data_hash
from modular_core import Candle
from modular_core.config import DataConfig, EngineConfig
from modular_strategy_fvg import FvgModule


class _PreparedBars:
    def __init__(self, bars: tuple[Candle, ...]) -> None:
        self._bars = bars
        self.total_bars = len(bars)
        self.first_candle = bars[0]
        self.last_candle = bars[-1]
        self.data_hash = incremental_candle_data_hash(bars)
        self.traversals = 0

    def iter_candles(self):
        self.traversals += 1
        return iter(self._bars)

    def iter_candles_from(self, start_index: int):
        self.traversals += 1
        return iter(self._bars[start_index:])


def _bars() -> tuple[Candle, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows = (
        (95.0, 100.0, 90.0, 95.0),
        (96.0, 102.0, 91.0, 97.0),
        (106.0, 110.0, 105.0, 107.0),
        (107.0, 111.0, 106.0, 108.0),
        (108.0, 112.0, 107.0, 109.0),
        (109.0, 113.0, 108.0, 110.0),
        (110.0, 114.0, 109.0, 111.0),
        (111.0, 115.0, 110.0, 112.0),
    )
    return tuple(
        Candle(
            timestamp=start + timedelta(minutes=15 * index),
            open=open_,
            high=high,
            low=low,
            close=close,
            volume=10.0 + index,
            symbol="BTCUSDT",
            timeframe="15m",
        )
        for index, (open_, high, low, close) in enumerate(rows)
    )


def _engine() -> ScalableBacktestEngine:
    return ScalableBacktestEngine(
        FvgModule(timeframes=("15m",)),
        EngineConfig(
            data=DataConfig(
                expected_interval_seconds=900,
                gap_policy="reject",
                max_base_history=64,
            ),
        ),
        performance=PerformanceConfig(
            mode="canonical_audit",
            max_memory_mb=None,
            progress_interval_bars=10_000,
            max_base_history=64,
            max_timeframe_history=64,
        ),
    )


def test_runtime_snapshot_restores_active_fvg_without_duplicate_publish() -> None:
    source = _PreparedBars(_bars())

    run = run_with_runtime_snapshot_restart(
        _engine,
        source,
        restart_at_index=3,
        seed=17,
    )
    try:
        assert run.restart_verified is True
        assert run.recovery_mode == "runtime_snapshot_v1"
        assert run.simulation_passes == 1
        assert run.source_traversals == 2
        assert source.traversals == 2
        assert run.source_rows_replayed == 0
        assert run.checkpoint is not None
        fvg_state = next(state for state in run.checkpoint.kernel.module_states if state.module == "fvg")
        assert fvg_state.payload["zones"]
        assert run.result.bars_processed == len(source._bars)
    finally:
        journal = run.result.journal
        if isinstance(journal, ScalableJournal):
            journal.streaming_store.cleanup()
