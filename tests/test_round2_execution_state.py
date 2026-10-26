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


class PersistentStopLimit(StrategyModule):
    name = "persistent_stop_limit"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.STOP_LIMIT,
                        quantity=1,
                        trigger_price=105,
                        limit_price=103,
                        stop_loss=95,
                        take_profit=120,
                    ),
                )
            )
        return ModuleDecision()


def test_stop_limit_remains_triggered_across_bars(bars_factory) -> None:
    bars = bars_factory(
        [
            (100, 101, 99, 100),
            (104, 106, 104, 105),  # trigger, but limit 103 is not touched
            (104, 104, 102, 103),  # no longer reaches trigger, but resting limit fills
            (103, 104, 102, 103),
        ]
    )
    result = BacktestEngine(PersistentStopLimit()).run(bars)
    entry = next(fill for fill in result.journal.fills if fill.reason == "entry")
    assert entry.timestamp == bars[2].timestamp
    assert entry.price == 103
    assert any(event.event == "triggered" for event in result.journal.orders)


class FeeAccountingModule(StrategyModule):
    name = "fee_accounting"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(orders=(OrderRequest(Side.BUY, OrderType.MARKET, quantity=2),))
        if view.index == 1 and view.position:
            return ModuleDecision(
                orders=(OrderRequest(Side.SELL, OrderType.MARKET, quantity=1, reduce_only=True),)
            )
        return ModuleDecision()


def test_reduce_order_does_not_double_count_allocated_entry_fee(bars_factory) -> None:
    cfg = EngineConfig(close_open_position_at_end=False)
    result = BacktestEngine(FeeAccountingModule(), cfg).run(bars_factory([100, 100, 100]))
    reduce_order = next(
        order
        for order in result.journal.orders
        if order.order.reduce_only and order.event == "filled"
    )
    expected_exit_fee = 100 * 1 * cfg.fees.taker_rate
    assert reduce_order.order.filled_quantity == 1
    assert reduce_order.order.fees_paid == pytest.approx(expected_exit_fee)
    trade = result.journal.trades[0]
    expected_entry_fee_share = 100 * 1 * cfg.fees.taker_rate
    assert trade.fees == pytest.approx(expected_entry_fee_share + expected_exit_fee)


def test_stop_limit_same_bar_policy_can_be_explicitly_optimistic(bars_factory) -> None:
    cfg = EngineConfig(execution=ExecutionConfig(stop_limit_same_bar_policy="fill"))
    bars = bars_factory([(100, 101, 99, 100), (104, 107, 102, 106), (106, 107, 105, 106)])
    result = BacktestEngine(PersistentStopLimit(), cfg).run(bars)
    entry = next(fill for fill in result.journal.fills if fill.reason == "entry")
    assert entry.timestamp == bars[1].timestamp
