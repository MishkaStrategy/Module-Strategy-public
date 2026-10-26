from datetime import timedelta

from modular_core import (
    BacktestEngine,
    EngineConfig,
    ExchangeRulesAdapter,
    ExecutionConfig,
    InstrumentSpec,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
    TimeInForce,
)


def test_instrument_rules_round_by_price_purpose():
    rules = ExchangeRulesAdapter(
        InstrumentSpec("BTCUSDT", tick_size=0.1, quantity_step=0.001, min_quantity=0.001)
    )
    assert rules.normalize_entry_price(100.09, Side.BUY, OrderType.LIMIT) == 100.0
    assert rules.normalize_stop_price(95.09, entry_price=100, side=Side.BUY) == 95.0
    assert rules.normalize_target_price(105.09, entry_price=100, side=Side.BUY) == 105.0
    assert rules.normalize_quantity(1.2349, price=100) == 1.234


def test_post_only_marketable_order_is_rejected(bars_factory):
    class PostOnly(StrategyModule):
        name = "post_only"

        def on_bar(self, view: MarketView) -> ModuleDecision:
            if view.index == 0:
                return ModuleDecision(
                    orders=(
                        OrderRequest(
                            Side.BUY,
                            OrderType.LIMIT,
                            quantity=1,
                            price=101,
                            post_only=True,
                        ),
                    )
                )
            return ModuleDecision()

    result = BacktestEngine(PostOnly()).run(bars_factory([100, 101]))
    assert any(row["reason"] == "post_only_rejected" for row in result.journal.skips)


class PartialEntry(StrategyModule):
    name = "partial"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.LIMIT,
                        quantity=4,
                        price=100,
                        stop_loss=95,
                        take_profit=120,
                        time_in_force=TimeInForce.GTC,
                    ),
                )
            )
        return ModuleDecision()


def test_partial_fill_keeps_order_open_and_builds_position_from_fills(bars_factory):
    config = EngineConfig(
        close_open_position_at_end=False,
        execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
    )
    result = BacktestEngine(PartialEntry(), config).run(bars_factory([(100, 101, 99, 100)] * 4))
    entry_fills = [fill for fill in result.journal.fills if fill.reason == "entry"]
    assert [fill.quantity for fill in entry_fills] == [2.0, 1.0, 0.5]
    partial_events = [e for e in result.journal.orders if e.event == "partially_filled"]
    assert len(partial_events) == 3
    assert partial_events[-1].order.remaining_quantity == 0.5


def test_gtd_order_expires_by_time(bars_factory):
    class GTD(StrategyModule):
        name = "gtd"

        def on_bar(self, view: MarketView) -> ModuleDecision:
            if view.index == 0:
                return ModuleDecision(
                    orders=(
                        OrderRequest(
                            Side.BUY,
                            OrderType.LIMIT,
                            quantity=1,
                            price=90,
                            time_in_force=TimeInForce.GTD,
                            good_till_time=view.current.timestamp + timedelta(minutes=6),
                        ),
                    )
                )
            return ModuleDecision()

    result = BacktestEngine(GTD()).run(bars_factory([100, 101, 102]))
    assert any(e.event == "expired" for e in result.journal.orders)
