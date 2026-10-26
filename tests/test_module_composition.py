from dataclasses import FrozenInstanceError

from modular_core import (
    BacktestEngine,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)


class Producer(StrategyModule):
    name = "producer"

    def __init__(self):
        self.events = []
        self.trades = []

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
                ),
                diagnostics={"state": "armed"},
            )
        return ModuleDecision()

    def on_order_event(self, event):
        self.events.append(event.event)

    def on_trade(self, trade):
        self.trades.append(trade.exit_reason)


class Observer(StrategyModule):
    name = "observer"

    def __init__(self):
        self.pending_seen = None
        self.position_is_frozen = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            self.pending_seen = view.pending_order_count
        if view.position is not None:
            try:
                view.position.quantity = 999
            except FrozenInstanceError:
                self.position_is_frozen = True
        return ModuleDecision()


def test_modules_are_composed_in_order_and_receive_events(bars_factory):
    producer = Producer()
    observer = Observer()
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 99, 105)])
    result = BacktestEngine([producer, observer]).run(bars)
    assert observer.pending_seen == 1
    assert observer.position_is_frozen
    assert producer.events[:2] == ["submitted", "filled"]
    assert producer.trades == ["target"]
    assert result.modules == ("producer", "observer")
    assert result.journal.diagnostics[0]["values"] == {"state": "armed"}
