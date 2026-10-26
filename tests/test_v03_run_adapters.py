from modular_core import (
    BacktestEngine,
    HistoricalDataFeed,
    MarketView,
    ModuleDecision,
    StrategyModule,
)


class Noop(StrategyModule):
    name = "noop_v03"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_historical_datafeed_is_accepted(bars_factory):
    bars = bars_factory([100, 101])
    result = BacktestEngine(Noop()).run(HistoricalDataFeed(bars))
    assert result.bars_processed == 2


def test_run_hashes_are_reproducible(bars_factory):
    bars = bars_factory([100, 101])
    first = BacktestEngine(Noop()).run(bars).run_manifest
    second = BacktestEngine(Noop()).run(bars).run_manifest
    assert first.run_id == second.run_id
    assert first.data_hash == second.data_hash
    assert first.config_hash == second.config_hash
    assert first.module_hash == second.module_hash
