from __future__ import annotations

from datetime import UTC, datetime, timedelta
from importlib.metadata import entry_points

import pytest
from modular_core import (
    BacktestEngine,
    Candle,
    MarketView,
    ModuleDecision,
    ModuleRegistry,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
    restore_module_states,
)

from modular_strategy_position_exit_management import (
    MODULE_NAME,
    PositionExitManagementModule,
    PositionExitReason,
)

START = datetime(2026, 1, 1, tzinfo=UTC)


def candles(rows: list[tuple[float, float, float, float]]) -> list[Candle]:
    return [
        Candle(
            START + timedelta(minutes=5 * index),
            open_,
            high,
            low,
            close,
            1.0,
            "BTCUSDT",
            "5m",
        )
        for index, (open_, high, low, close) in enumerate(rows)
    ]


def position_exit_registry() -> ModuleRegistry:
    selected = tuple(
        entry_points().select(
            group="modular_strategy.modules",
            name=MODULE_NAME,
        )
    )
    assert len(selected) == 1
    registry = ModuleRegistry()
    registry.register(MODULE_NAME, selected[0].load())
    return registry


class OneShotEntry(StrategyModule):
    name = "position_exit_test_entry"

    def __init__(
        self,
        *,
        quantity: float = 1.0,
        take_profit: float | None = None,
        side: Side = Side.BUY,
    ) -> None:
        self.quantity = quantity
        self.take_profit = take_profit
        self.side = side

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index != 0:
            return ModuleDecision()
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=self.side,
                    order_type=OrderType.MARKET,
                    quantity=self.quantity,
                    stop_loss=95.0 if self.side is Side.BUY else 105.0,
                    take_profit=self.take_profit,
                    tag="position-exit-integration",
                ),
            )
        )


def test_time_exit_runs_through_real_engine_on_next_bar_open():
    exit_module = PositionExitManagementModule(max_holding_minutes=10)
    result = BacktestEngine((OneShotEntry(), exit_module)).run(
        candles(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 103, 100, 102),
                (103, 104, 102, 103),
            ]
        )
    )
    assert len(result.journal.trades) == 1
    trade = result.journal.trades[0]
    assert trade.exit_reason == PositionExitReason.TIME_EXIT.value
    assert trade.exit_price == pytest.approx(103)
    assert trade.position_closed is True


def test_partial_exit_then_time_exit_create_two_trade_slices():
    exit_module = PositionExitManagementModule(
        max_holding_minutes=20,
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
    )
    result = BacktestEngine((OneShotEntry(quantity=4), exit_module)).run(
        candles(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 106, 100, 105),
                (106, 107, 104, 106),
                (106, 107, 105, 106),
                (107, 108, 106, 107),
            ]
        )
    )
    assert [trade.quantity for trade in result.journal.trades] == pytest.approx([2, 2])
    assert [trade.exit_reason for trade in result.journal.trades] == [
        f"{PositionExitReason.PARTIAL_EXIT.value}.stage_1",
        PositionExitReason.TIME_EXIT.value,
    ]
    assert [trade.position_closed for trade in result.journal.trades] == [False, True]


def test_breakeven_update_preserves_existing_take_profit_in_real_ledger():
    exit_module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    result = BacktestEngine((OneShotEntry(take_profit=120), exit_module)).run(
        candles(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 106, 100, 105),
                (105, 121, 101, 119),
            ]
        )
    )
    assert len(result.journal.trades) == 1
    trade = result.journal.trades[0]
    assert trade.exit_reason == "target"
    assert trade.exit_price == pytest.approx(120)


def test_real_kernel_checkpoint_captures_and_restores_module_state():
    exit_module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    result = BacktestEngine((OneShotEntry(quantity=4), exit_module)).run(
        candles(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 106, 100, 105),
                (105, 106, 104, 105),
            ]
        )
    )
    module_state = next(
        state for state in result.checkpoint.module_states if state.module == MODULE_NAME
    )
    restored = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    restore_module_states((restored,), (module_state,))
    assert restored.snapshot_state() == module_state.payload


def test_registry_discovers_installed_entry_point():
    registry = position_exit_registry()
    module = registry.load_named(MODULE_NAME, {"max_holding_minutes": 60})
    assert isinstance(module, PositionExitManagementModule)


def test_round_17_short_partial_and_breakeven_run_through_real_engine():
    exit_module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    result = BacktestEngine((OneShotEntry(quantity=4, side=Side.SELL), exit_module)).run(
        candles(
            [
                (100, 101, 99, 100),
                (100, 101, 99, 100),
                (99, 100, 94, 95),
                (94, 96, 93, 94),
                (94, 95, 92, 93),
            ]
        )
    )
    assert len(result.journal.trades) == 2
    partial, terminal = result.journal.trades
    assert partial.quantity == pytest.approx(2)
    assert partial.exit_reason.endswith("partial_exit.stage_1")
    assert partial.position_closed is False
    assert terminal.quantity == pytest.approx(2)
    assert terminal.exit_reason == "end_of_data"
    assert terminal.position_closed is True
    assert result.checkpoint.module_states


def test_round_18_registry_plan_engine_checkpoint_contract():
    registry = position_exit_registry()
    module = registry.load_named(
        MODULE_NAME,
        {
            "max_holding_minutes": 10,
            "breakeven_trigger_r": "1.0",
            "minimum_stop_move_bps": "0",
            "market_buffer_bps": "0",
        },
    )
    plan = registry.validate_and_plan((OneShotEntry(), module), base_timeframe="5m")
    assert plan.modules[-1] is module
    result = BacktestEngine(plan.modules).run(
        candles(
            [
                (100, 101, 99, 100),
                (100, 102, 99, 101),
                (101, 106, 100, 105),
                (105, 106, 104, 105),
            ]
        )
    )
    saved = next(item for item in result.checkpoint.module_states if item.module == MODULE_NAME)
    assert saved.module_version == "0.2.1"
    assert saved.schema_version == "2"

    restored = registry.load_named(
        MODULE_NAME,
        {
            "max_holding_minutes": 10,
            "breakeven_trigger_r": "1",
            "minimum_stop_move_bps": 0,
            "market_buffer_bps": 0,
        },
    )
    restore_module_states((restored,), (saved,))
    assert restored.snapshot_state() == saved.payload
