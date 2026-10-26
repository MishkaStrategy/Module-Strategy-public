import math

from modular_core import (
    BacktestEngine,
    EngineConfig,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    RiskConfig,
    Side,
    StrategyModule,
)


class RiskSizedTrade(StrategyModule):
    name = "risk_sized"

    def __init__(self):
        self.done = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not self.done:
            self.done = True
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.MARKET,
                        stop_loss=99.9,
                        take_profit=102,
                        risk_fraction=0.005,
                    ),
                )
            )
        return ModuleDecision()


def test_fixed_fractional_sizing_and_metrics(bars_factory):
    config = EngineConfig(
        initial_cash=10_000,
        risk=RiskConfig(default_risk_fraction=0.005, max_notional_multiple=3.0),
    )
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99.5, 100), (100, 103, 100, 102)])
    result = BacktestEngine(RiskSizedTrade(), config).run(bars)
    trade = result.journal.trades[0]
    # Raw risk sizing is 500 units, but the 3x notional cap limits quantity to 300.
    assert trade.quantity == 300
    assert result.metrics.trades == 1
    assert result.metrics.wins == 1
    assert result.metrics.profit_factor == math.inf
    assert result.metrics.max_drawdown_pct >= 0


def test_forced_final_close_does_not_duplicate_equity_point(bars_factory):
    config = EngineConfig(initial_cash=10_000)
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99, 100)])
    result = BacktestEngine(RiskSizedTrade(), config).run(bars)
    assert len(result.journal.equity) == len(bars)
    assert result.metrics.exposure_pct == 50.0
