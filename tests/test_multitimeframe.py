from datetime import UTC, datetime, timedelta

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    DataConfig,
    EngineConfig,
    MarketView,
    ModuleDecision,
    StrategyModule,
    TimeframeSpec,
)
from modular_core.data import DataValidationError


class MtfProbe(StrategyModule):
    name = "mtf_probe"

    def __init__(self):
        self.htf_closes = []
        self.gap_indexes = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        current = view.current_for("15m")
        self.htf_closes.append(current.close if current else None)
        if view.data_gap:
            self.gap_indexes.append(view.index)
        return ModuleDecision()


def test_engine_exposes_only_completed_higher_timeframe(bars_factory):
    module = MtfProbe()
    bars = bars_factory([(100, 101, 99, 100), (100, 102, 99, 101), (101, 103, 100, 102)])
    engine = BacktestEngine(module, timeframes=(TimeframeSpec(3, "15m"),))
    engine.run(bars)
    assert module.htf_closes == [None, None, 102]


def test_gap_safe_aggregator_resets_incomplete_group():
    start = datetime(2026, 1, 1, tzinfo=UTC)
    offsets = [0, 5, 15, 20, 25]
    bars = [
        Candle(
            start + timedelta(minutes=offset),
            100 + i,
            102 + i,
            99 + i,
            101 + i,
            1,
            "BTCUSDT",
            "5m",
        )
        for i, offset in enumerate(offsets)
    ]
    module = MtfProbe()
    BacktestEngine(module, timeframes=(TimeframeSpec(3, "15m", gap_safe=True),)).run(bars)
    assert module.gap_indexes == [2]
    assert module.htf_closes[-1] == bars[-1].close
    assert all(value is None for value in module.htf_closes[:-1])


def test_data_gap_can_be_rejected(bars_factory):
    bars = bars_factory([100, 101, 102])
    bars = [bars[0], bars[2]]
    config = EngineConfig(data=DataConfig(gap_policy="reject"))
    with pytest.raises(DataValidationError, match="data gap"):
        BacktestEngine(MtfProbe(), config).run(bars)
