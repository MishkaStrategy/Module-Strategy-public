from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    EngineConfig,
    ExecutionConfig,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)
from modular_core.audit import Journal
from modular_core.config import FeeConfig
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec
from modular_core.models import Fill, Position, PositionSide, TimeInForce

UTC = timezone.utc
NOW = datetime(2026, 1, 1, tzinfo=UTC)


def bar(minutes: int, o: float, h: float, low: float, c: float) -> Candle:
    return Candle(NOW + timedelta(minutes=5 * minutes), o, h, low, c, 1, "X", "5m")


def broker(*, contract_size: float = 1.0, config: EngineConfig | None = None) -> BrokerSimulator:
    return BrokerSimulator(
        config or EngineConfig(fees=FeeConfig(0, 0)),
        Journal(run_id="run"),
        InstrumentSpec(symbol="X", contract_size=contract_size),
    )


def seed_long(value: BrokerSimulator, *, quantity: float = 1, price: float = 100) -> None:
    value.portfolio.position = Position(
        id="p1",
        entry_order_id="entry",
        side=PositionSide.LONG,
        quantity=quantity,
        entry_price=price,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0,
        entry_fills=[
            Fill(
                order_id="entry",
                timestamp=NOW,
                side=Side.BUY,
                quantity=quantity,
                price=price,
                fee=0,
                slippage=0,
                reason="entry",
                liquidity="taker",
            )
        ],
    )


def test_take_profit_market_sell_triggers_on_high_not_low() -> None:
    value = broker()
    seed_long(value)
    order = value.submit(
        OrderRequest(
            side=Side.SELL,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            quantity=1,
            trigger_price=110,
            reduce_only=True,
        ),
        source_module="exit",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )
    assert order is not None
    value.process_bar(bar(1, 100, 111, 99, 105), 1)
    assert value.portfolio.position is None
    assert value.journal.trades[-1].exit_price == 110


def test_take_profit_market_short_gap_uses_favorable_open_only_when_already_beyond_trigger() -> (
    None
):
    value = broker()
    value.portfolio.position = Position(
        id="p1",
        entry_order_id="entry",
        side=PositionSide.SHORT,
        quantity=1,
        entry_price=100,
        entry_time=NOW,
        entry_index=0,
        entry_fee=0,
        entry_fills=[
            Fill(
                order_id="entry",
                timestamp=NOW,
                side=Side.SELL,
                quantity=1,
                price=100,
                fee=0,
                slippage=0,
                reason="entry",
                liquidity="taker",
            )
        ],
    )
    value.submit(
        OrderRequest(
            Side.BUY, OrderType.TAKE_PROFIT_MARKET, quantity=1, trigger_price=90, reduce_only=True
        ),
        source_module="exit",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )
    value.process_bar(bar(1, 85, 91, 84, 88), 1)
    assert value.journal.trades[-1].exit_price == 85


def test_gtd_expires_at_exact_good_till_time_before_fill() -> None:
    value = broker()
    good_till = NOW + timedelta(minutes=5)
    order = value.submit(
        OrderRequest(
            Side.BUY,
            OrderType.LIMIT,
            quantity=1,
            price=100,
            stop_loss=90,
            take_profit=110,
            time_in_force=TimeInForce.GTD,
            good_till_time=good_till,
        ),
        source_module="entry",
        created_at=NOW,
        current_index=0,
        mark_price=101,
    )
    assert order is not None
    value.process_bar(bar(1, 101, 102, 99, 100), 1)
    assert order.status.value == "expired"
    assert value.portfolio.position is None


class OneLimit(StrategyModule):
    name = "one_limit"

    def __init__(self) -> None:
        self.done = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if self.done:
            return ModuleDecision()
        self.done = True
        return ModuleDecision(
            orders=(
                OrderRequest(
                    Side.BUY,
                    OrderType.LIMIT,
                    quantity=1,
                    price=100,
                    stop_loss=95,
                    take_profit=105,
                ),
            )
        )


def test_limit_entry_never_reuses_unknown_pre_entry_extreme_for_bracket() -> None:
    bars = (
        bar(0, 100, 101, 99, 100),
        bar(1, 110, 111, 94, 100),
    )
    result = BacktestEngine(
        OneLimit(),
        EngineConfig(
            fees=FeeConfig(0, 0),
            execution=ExecutionConfig(allow_exit_on_entry_bar=True, intrabar_priority="stop"),
        ),
        instrument=InstrumentSpec(symbol="X"),
    ).run(bars)
    assert result.journal.trades[-1].exit_reason == "end_of_data"


def test_contract_size_applies_to_pnl_and_fees() -> None:
    value = broker(contract_size=10)
    seed_long(value, quantity=2, price=100)
    trade = value.close_position(NOW + timedelta(minutes=5), 110, "test", quantity=2)
    assert trade is not None
    assert trade.gross_pnl == pytest.approx(200)
    assert trade.net_pnl == pytest.approx(200)
    assert value.portfolio.cash == pytest.approx(10_200)


def test_full_close_cancels_other_reduce_only_orders() -> None:
    value = broker()
    seed_long(value)
    first = value.submit(
        OrderRequest(Side.SELL, OrderType.LIMIT, quantity=1, price=110, reduce_only=True),
        source_module="exit",
        created_at=NOW,
        current_index=0,
        mark_price=100,
    )
    assert first is not None
    value.close_position(NOW + timedelta(minutes=5), 105, "manual")
    assert first.status.value == "cancelled"
    assert value.pending_orders == []


def test_direct_close_rejects_nonfinite_boundaries() -> None:
    value = broker()
    seed_long(value)
    with pytest.raises(ValueError, match="finite"):
        value.close_position(NOW, float("nan"), "bad")
