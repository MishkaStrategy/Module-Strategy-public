from __future__ import annotations

import pytest
from modular_core import PositionSide, Trade

from modular_strategy_position_exit_management import (
    ManagementAction,
    PositionExitManagementModule,
    PositionExitReason,
)


def test_stable_noop_fast_path_returns_empty_routine_decision(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(emit_status_feature=False)
    first = module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    second = module.on_bar(view_factory(1, candle=candle_factory(1), position=position_factory()))
    assert not first.closes and not first.position_updates and not first.cancellations
    assert second == type(second)()
    assert module.snapshot_state()["active_position"]["bars_managed"] == 2


def test_fast_path_never_hides_future_entry_time(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    position = position_factory(entry_minutes=10)
    decision = module.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    assert decision.skips == (PositionExitReason.FUTURE_ENTRY_TIME.value,)


def test_fast_path_respects_disabled_scale_in_reset(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(reset_on_scale_in=False)
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory(quantity=1)))
    decision = module.on_bar(
        view_factory(1, candle=candle_factory(1), position=position_factory(quantity=2))
    )
    assert decision == type(decision)()
    state = module.snapshot_state()["active_position"]
    assert state["initial_quantity"] == "1"
    assert state["last_seen_quantity"] == "2"


def test_trade_callback_clears_pending_exit_flags(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=1)
    position = position_factory()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    module.on_bar(view_factory(1, candle=candle_factory(1), position=position))
    before = module.snapshot_state()["active_position"]
    assert before["full_close_requested"] is True
    module.on_trade(
        Trade(
            entry_time=position.entry_time,
            exit_time=candle_factory(2).close_time,
            side=position.side,
            entry_price=position.entry_price,
            exit_price=position.entry_price,
            quantity=position.quantity,
            gross_pnl=0.0,
            fees=0.0,
            net_pnl=0.0,
            exit_reason="test",
            tag=position.tag,
            setup_id=position.setup_id,
            position_id=position.id,
            position_closed=True,
        )
    )
    after = module.snapshot_state()["active_position"]
    assert after["full_close_requested"] is False
    assert after["pending_partial_index"] is None


def test_short_percent_trailing_is_directionally_correct(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.05",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=81, high=82, low=80, close=81),
            position=position_factory(side=PositionSide.SHORT, stop_loss=105),
        )
    )
    assert decision.position_updates[0].stop_loss == pytest.approx(84)
    assert decision.position_updates[0].stop_loss > 80


def test_combined_breakeven_and_trailing_selects_tighter_stop(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        trailing_mode="r_multiple",
        trailing_activation_r=1,
        trailing_distance_r="0.5",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=105, high=110, low=104, close=108),
            position=position_factory(stop_loss=95),
        )
    )
    update = decision.position_updates[0]
    assert update.stop_loss == pytest.approx(107.5)
    assert decision.diagnostics["stop_components"] == (
        ManagementAction.BREAKEVEN.value,
        ManagementAction.TRAILING.value,
    )


def test_action_status_is_emitted_even_when_routine_status_is_disabled(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=1,
        emit_status_feature=False,
        minimum_stop_move_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=105, high=106, low=104, close=105),
            position=position_factory(stop_loss=95),
        )
    )
    assert decision.position_updates
    assert len(decision.features) == 1
    assert decision.features[0].value["reason"] == PositionExitReason.PROTECTIVE_STOP_UPDATE.value


def test_zero_market_buffer_still_keeps_stop_strictly_behind_mark(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        breakeven_trigger_r=0,
        market_buffer_bps=0,
        minimum_stop_move_bps=0,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=101, low=99, close=100),
            position=position_factory(entry_price=100, stop_loss=95),
        )
    )
    assert decision.position_updates[0].stop_loss < 100
