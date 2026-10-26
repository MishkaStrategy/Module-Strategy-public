from modular_core import (
    BacktestEngine,
    EngineConfig,
    MarketView,
    ModuleDecision,
    StrategyModule,
)


class BrokenStart(StrategyModule):
    name = "broken_start"

    def on_start(self) -> None:
        raise RuntimeError("start failed")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        raise AssertionError("must not be called")


class Healthy(StrategyModule):
    name = "healthy"

    def __init__(self):
        self.calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision()


def test_non_strict_start_failure_disables_only_failed_module(bars_factory):
    healthy = Healthy()
    config = EngineConfig(strict_module_errors=False)
    result = BacktestEngine([BrokenStart(), healthy], config).run(bars_factory([100, 101]))
    assert healthy.calls == 2
    assert result.modules == ("healthy",)
    assert result.journal.errors[0]["stage"] == "module_start:broken_start"
