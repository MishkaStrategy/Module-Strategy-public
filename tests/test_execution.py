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


class OneShotMarket(StrategyModule):
    name = "one_shot"

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
                        quantity=1,
                        stop_loss=95,
                        take_profit=110,
                    ),
                )
            )
        return ModuleDecision()


def test_market_order_fills_on_next_bar_open(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (103, 104, 102, 103), (104, 111, 103, 110)])
    result = BacktestEngine(OneShotMarket()).run(bars)
    assert result.journal.trades[0].entry_price == 103
    assert result.journal.trades[0].exit_reason == "target"


class AmbiguousBracket(StrategyModule):
    name = "ambiguous"

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
                        quantity=1,
                        stop_loss=95,
                        take_profit=105,
                    ),
                )
            )
        return ModuleDecision()


def test_same_bar_stop_target_uses_configured_priority(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99, 100), (100, 106, 94, 100)])
    config = EngineConfig(execution=ExecutionConfig(intrabar_priority="stop"))
    result = BacktestEngine(AmbiguousBracket(), config).run(bars)
    assert result.journal.trades[0].exit_reason == "stop"


class DoubleSubmit(StrategyModule):
    name = "double"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            req = OrderRequest(Side.BUY, OrderType.LIMIT, quantity=1, price=90)
            return ModuleDecision(orders=(req, req))
        return ModuleDecision()


def test_one_active_order_policy_rejects_second_order(bars_factory):
    result = BacktestEngine(DoubleSubmit()).run(bars_factory([100, 101]))
    reasons = [x["reason"] for x in result.journal.skips]
    assert "active_order_exists" in reasons


class PositionLifecycleProbe(StrategyModule):
    name = "position_lifecycle_probe"

    def __init__(self):
        self.observed = None

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.MARKET,
                        quantity=1,
                        tag="lifecycle",
                        metadata={
                            "router_max_holding_seconds": 3600,
                            "router_no_progress_after_seconds": 1800,
                            "router_no_progress_minimum_mfe_r": "0.5",
                        },
                    ),
                )
            )
        if view.position is not None and self.observed is None:
            self.observed = view.position
        return ModuleDecision()


def test_position_snapshot_preserves_entry_index_and_metadata(bars_factory):
    module = PositionLifecycleProbe()

    BacktestEngine(module).run(bars_factory([100, 101, 102]))

    snapshot = module.observed
    assert snapshot is not None
    assert snapshot.entry_index == 1
    assert snapshot.metadata == {
        "router_max_holding_seconds": 3600,
        "router_no_progress_after_seconds": 1800,
        "router_no_progress_minimum_mfe_r": "0.5",
    }
