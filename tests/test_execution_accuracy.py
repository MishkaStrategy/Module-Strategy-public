import pytest

from modular_core import (
    BacktestEngine,
    EngineConfig,
    ExecutionConfig,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)


class OneMarket(StrategyModule):
    name = "one_market"

    def __init__(self, *, stop=95, target=105, quantity=1, risk_fraction=None):
        self.done = False
        self.stop = stop
        self.target = target
        self.quantity = quantity
        self.risk_fraction = risk_fraction

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self.done:
            return ModuleDecision()
        self.done = True
        return ModuleDecision(
            orders=(
                OrderRequest(
                    Side.BUY,
                    OrderType.MARKET,
                    quantity=self.quantity,
                    stop_loss=self.stop,
                    take_profit=self.target,
                    risk_fraction=self.risk_fraction,
                ),
            )
        )


def test_stop_gap_uses_worse_open_price(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99, 100), (90, 92, 89, 91)])
    result = BacktestEngine(OneMarket(target=110)).run(bars)
    assert result.journal.trades[0].exit_price == 90


def test_entry_bar_bracket_is_not_replayed_by_default(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (100, 110, 90, 100)])
    result = BacktestEngine(OneMarket()).run(bars)
    assert result.journal.trades[0].exit_reason == "end_of_data"


def test_entry_bar_bracket_can_be_enabled_explicitly(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (100, 110, 90, 100)])
    config = EngineConfig(
        execution=ExecutionConfig(allow_exit_on_entry_bar=True, intrabar_priority="stop")
    )
    result = BacktestEngine(OneMarket(), config).run(bars)
    assert result.journal.trades[0].exit_reason == "stop"


def test_market_gap_that_breaks_bracket_is_rejected(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (90, 91, 89, 90)])
    result = BacktestEngine(OneMarket(stop=95, target=110)).run(bars)
    assert not result.journal.fills
    assert any(
        event.event == "rejected" and event.reason == "invalid_bracket"
        for event in result.journal.orders
    )


def test_risk_size_is_reduced_at_actual_market_fill(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (110, 111, 109, 110), (110, 121, 109, 120)])
    module = OneMarket(stop=99, target=120, quantity=None, risk_fraction=0.005)
    result = BacktestEngine(module).run(bars)
    entry = next(fill for fill in result.journal.fills if fill.reason == "entry")
    assert entry.quantity == pytest.approx(50 / 11)


class TwoQueuedMarkets(StrategyModule):
    name = "two_queued"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.MARKET,
                        quantity=1,
                        stop_loss=95,
                        take_profit=105,
                    ),
                    OrderRequest(
                        Side.BUY,
                        OrderType.MARKET,
                        quantity=1,
                        stop_loss=95,
                        take_profit=110,
                    ),
                )
            )
        return ModuleDecision()


def test_pending_entry_does_not_reenter_on_exit_bar_by_default(bars_factory):
    bars = bars_factory(
        [
            (100, 101, 99, 100),
            (100, 101, 99, 100),
            (100, 106, 99, 105),
            (103, 104, 102, 103),
        ]
    )
    config = EngineConfig(one_active_order=False)
    result = BacktestEngine(TwoQueuedMarkets(), config).run(bars)
    entries = [fill for fill in result.journal.fills if fill.reason == "entry"]
    assert [fill.timestamp for fill in entries] == [
        bars[1].timestamp,
        bars[3].timestamp,
    ]


def test_explicit_quantity_and_risk_fraction_are_rejected():
    with pytest.raises(ValueError, match="mutually exclusive"):
        OrderRequest(
            Side.BUY,
            OrderType.MARKET,
            quantity=1,
            stop_loss=95,
            risk_fraction=0.005,
        )
