import pytest

from modular_core import (
    BacktestEngine,
    DataConfig,
    EngineConfig,
    MarketView,
    ModuleDecision,
    StrategyModule,
    TimeframeSpec,
)
from modular_core.data import DataValidationError


class Noop(StrategyModule):
    name = "noop"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_duplicate_candle_is_rejected(bars_factory):
    bars = bars_factory([100, 101])
    bars[1] = bars[0]
    with pytest.raises(DataValidationError, match="duplicate"):
        BacktestEngine(Noop()).run(bars)


def test_module_never_sees_future_bars(bars_factory):
    seen = []

    class Probe(StrategyModule):
        name = "probe"

        def on_bar(self, view: MarketView) -> ModuleDecision:
            seen.append((view.index, len(view.bars), view.current.timestamp))
            assert len(view.bars) == view.index + 1
            return ModuleDecision()

    bars = bars_factory([100, 101, 102])
    BacktestEngine(Probe()).run(bars)
    assert [x[:2] for x in seen] == [(0, 1), (1, 2), (2, 3)]


def test_saved_market_view_never_gains_future_bars(bars_factory):
    saved = []

    class SaveFirstView(StrategyModule):
        name = "save_first"

        def on_bar(self, view: MarketView) -> ModuleDecision:
            if view.index == 0:
                saved.append(view)
            return ModuleDecision()

    BacktestEngine(SaveFirstView()).run(bars_factory([100, 101, 102, 103]))
    assert len(saved[0].bars) == 1
    assert saved[0].current.close == 100


def test_modules_share_one_immutable_snapshot_per_bar(bars_factory):
    class Capture(StrategyModule):
        def __init__(self, name):
            self.name = name
            self.views = []

        def on_bar(self, view: MarketView) -> ModuleDecision:
            self.views.append(view)
            return ModuleDecision()

    first = Capture("first")
    second = Capture("second")
    bars = bars_factory([100, 101, 102, 103, 104, 105])
    BacktestEngine(
        (first, second),
        timeframes=(TimeframeSpec(3, "15m", max_history=2),),
    ).run(bars)

    assert len(first.views) == len(second.views) == len(bars)
    for first_view, second_view in zip(first.views, second.views, strict=True):
        assert first_view is second_view
        assert first_view.base_bars is second_view.base_bars
        assert first_view.timeframe_bars is second_view.timeframe_bars
    assert first.views[0] is not first.views[1]


def test_bounded_saved_view_remains_unchanged(bars_factory):
    saved = []

    class Capture(StrategyModule):
        name = "bounded_capture"

        def on_bar(self, view: MarketView) -> ModuleDecision:
            if view.index in {1, 4}:
                saved.append(view)
            return ModuleDecision()

    bars = bars_factory([100, 101, 102, 103, 104])
    config = EngineConfig(data=DataConfig(max_base_history=2))
    BacktestEngine(Capture(), config).run(bars)

    assert [bar.close for bar in saved[0].base_bars] == [100, 101]
    assert [bar.close for bar in saved[1].base_bars] == [103, 104]
    assert saved[0].current.close == 101
