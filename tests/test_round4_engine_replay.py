from modular_core import (
    BacktestEngine,
    MarketView,
    ModuleDecision,
    StrategyModule,
    verify_event_stream,
)


class Noop(StrategyModule):
    name = "noop_replay"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_engine_event_stream_is_replay_valid(bars_factory) -> None:
    result = BacktestEngine(Noop()).run(bars_factory([100, 101, 102]))
    report = verify_event_stream(result.journal.events)
    assert report.valid, report.errors
    assert report.events == len(result.journal.events)
