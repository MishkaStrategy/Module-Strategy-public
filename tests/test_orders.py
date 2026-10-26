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


class LimitOnce(StrategyModule):
    name = "limit_once"

    def __init__(self, side: Side, price: float):
        self.side = side
        self.price = price
        self.done = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self.done:
            return ModuleDecision()
        self.done = True
        return ModuleDecision(
            orders=(OrderRequest(self.side, OrderType.LIMIT, quantity=1, price=self.price),)
        )


def test_order_cannot_fill_on_creation_candle(bars_factory):
    bars = bars_factory([(100, 120, 80, 100), (100, 101, 99, 100)])
    result = BacktestEngine(LimitOnce(Side.BUY, 90)).run(bars)
    assert not result.journal.fills


def test_buy_limit_uses_conservative_limit_price_by_default(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (88, 91, 87, 90), (90, 91, 89, 90)])
    result = BacktestEngine(LimitOnce(Side.BUY, 90)).run(bars)
    assert result.journal.fills[0].price == 90


def test_limit_price_improvement_is_explicit_policy(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (88, 91, 87, 90), (90, 91, 89, 90)])
    config = EngineConfig(execution=ExecutionConfig(limit_fill_policy="price_improvement"))
    result = BacktestEngine(LimitOnce(Side.BUY, 90), config).run(bars)
    assert result.journal.fills[0].price == 88
