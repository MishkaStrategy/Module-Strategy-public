from datetime import timedelta

import pytest

from modular_core import (
    BacktestEngine,
    EngineConfig,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)


class ScaleModule(StrategyModule):
    name = "scale"

    def __init__(self):
        self.quantities = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.quantities.append(view.position.quantity if view.position else 0)
        if view.index == 0:
            return ModuleDecision(orders=(OrderRequest(Side.BUY, OrderType.MARKET, quantity=1),))
        if view.index == 1 and view.position is not None:
            return ModuleDecision(orders=(OrderRequest(Side.BUY, OrderType.MARKET, quantity=1),))
        return ModuleDecision()


def test_scale_in_uses_fill_ledger_when_explicitly_enabled(bars_factory):
    module = ScaleModule()
    config = EngineConfig(allow_scale_in=True, close_open_position_at_end=False)
    BacktestEngine(module, config).run(bars_factory([100, 101, 102, 103]))
    assert module.quantities[-1] == 2


class PartialReduce(StrategyModule):
    name = "partial_reduce"

    def __init__(self):
        self.remaining = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.remaining.append(view.position.quantity if view.position else 0)
        if view.index == 0:
            return ModuleDecision(orders=(OrderRequest(Side.BUY, OrderType.MARKET, quantity=4),))
        if view.index == 1 and view.position is not None:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.SELL,
                        OrderType.MARKET,
                        quantity=1.5,
                        reduce_only=True,
                    ),
                )
            )
        return ModuleDecision()


def test_reduce_only_partial_exit_keeps_position_open(bars_factory):
    module = PartialReduce()
    config = EngineConfig(close_open_position_at_end=False)
    result = BacktestEngine(module, config).run(bars_factory([100, 101, 102, 103]))
    assert module.remaining[-1] == 2.5
    assert result.journal.trades[0].quantity == 1.5
    assert result.journal.trades[0].position_closed is False


class GapProducer(StrategyModule):
    name = "gap_producer"
    manifest = ModuleManifest(name=name, produced_features=("gap.feature",))

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                features=(
                    Feature(
                        "gap.feature",
                        self.name,
                        view.current.close_time,
                        {"nested": [1, 2]},
                        symbol=view.current.symbol,
                    ),
                )
            )
        return ModuleDecision()


class GapConsumer(StrategyModule):
    name = "gap_consumer"
    manifest = ModuleManifest(name=name, required_features=("gap.feature",))

    def __init__(self):
        self.seen = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.seen.append(view.features.get("gap.feature"))
        return ModuleDecision()


def test_gap_invalidates_features_and_feature_values_are_deep_read_only(bars_factory):
    bars = bars_factory([100, 101])
    bars[1] = type(bars[1])(
        bars[1].timestamp + timedelta(minutes=5),
        bars[1].open,
        bars[1].high,
        bars[1].low,
        bars[1].close,
        bars[1].volume,
        bars[1].symbol,
        bars[1].timeframe,
    )
    consumer = GapConsumer()
    result = BacktestEngine((consumer, GapProducer())).run(bars)
    first = consumer.seen[0]
    with pytest.raises(TypeError):
        first["nested"] = ()
    assert first["nested"] == (1, 2)
    assert consumer.seen[1] is None
    assert any(row["action"] == "gap_invalidated" for row in result.journal.features)


class StopLimitModule(StrategyModule):
    name = "stop_limit"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.STOP_LIMIT,
                        quantity=1,
                        trigger_price=105,
                        limit_price=106,
                        stop_loss=95,
                        take_profit=120,
                    ),
                )
            )
        return ModuleDecision()


def test_stop_limit_dto_and_execution_path_are_supported(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (104, 107, 103, 106), (106, 107, 105, 106)])
    result = BacktestEngine(StopLimitModule()).run(bars)
    entry = next(fill for fill in result.journal.fills if fill.reason == "entry")
    assert entry.price == 106
