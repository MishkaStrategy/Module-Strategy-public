from __future__ import annotations

from datetime import timedelta

import pytest
from modular_core import PositionSide, Trade

from modular_strategy_position_exit_management import (
    ManagementAction,
    PositionExitManagementModule,
    PositionExitReason,
)


def test_routine_noop_fast_path_emits_no_objects_but_advances_state(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    first = module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    second = module.on_bar(view_factory(1, candle=candle_factory(1), position=position_factory()))
    assert first.features == ()
    assert first.closes == ()
    assert first.position_updates == ()
    assert first.cancellations == ()
    assert first.diagnostics["bars_managed"] == 1
    assert second.features == ()
    assert second.closes == ()
    assert second.position_updates == ()
    assert second.cancellations == ()
    assert second.diagnostics == {}
    state = module.snapshot_state()["active_position"]
    assert state["bars_managed"] == 2


def test_verbose_status_disables_routine_noop_fast_path(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(emit_status_feature=True)
    decision = module.on_bar(view_factory(0, candle=candle_factory(0), position=position_factory()))
    assert len(decision.features) == 1
    assert decision.features[0].value["reason"] == (PositionExitReason.POSITION_INITIALIZED.value)


def test_fast_path_does_not_bypass_future_entry_guard(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(entry_minutes=30),
        )
    )
    assert decision.skips == (PositionExitReason.FUTURE_ENTRY_TIME.value,)
    assert decision.features


def test_reset_on_scale_in_false_retains_original_quantity_basis(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        reset_on_scale_in=False,
    )
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(quantity=2),
        )
    )
    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4),
        )
    )
    assert decision.closes[0].quantity == pytest.approx(1)


def test_on_trade_ignores_other_position_and_clears_matching_close_state(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(max_holding_minutes=5)
    position = position_factory()
    module.on_bar(view_factory(0, candle=candle_factory(0), position=position))
    state_before = module.snapshot_state()

    unrelated = Trade(
        side=PositionSide.LONG,
        quantity=1,
        entry_time=position.entry_time,
        exit_time=position.entry_time + timedelta(minutes=5),
        entry_price=100,
        exit_price=101,
        gross_pnl=1,
        fees=0,
        net_pnl=1,
        exit_reason="other",
        position_id="other-position",
    )
    module.on_trade(unrelated)
    assert module.snapshot_state() == state_before

    closed = Trade(
        side=PositionSide.LONG,
        quantity=position.quantity,
        entry_time=position.entry_time,
        exit_time=position.entry_time + timedelta(minutes=5),
        entry_price=position.entry_price,
        exit_price=101,
        gross_pnl=4,
        fees=0,
        net_pnl=4,
        exit_reason="closed",
        position_id=position.id,
        position_closed=True,
    )
    module.on_trade(closed)
    active = module.snapshot_state()["active_position"]
    assert active["full_close_requested"] is False
    assert active["pending_partial_index"] is None


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
            candle=candle_factory(0, open_=83, high=92, low=80, close=82),
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
        trailing_activation_r=2,
        trailing_distance_r=1,
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
        emit_status_feature=True,
    )
    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=109, high=112, low=108, close=110),
            position=position_factory(),
        )
    )
    assert decision.position_updates[0].stop_loss == pytest.approx(107)
    assert decision.features[0].value["action"] == ManagementAction.TRAILING.value
    assert decision.features[0].value["breakeven_applied"] is True
    assert decision.features[0].value["trailing_active"] is True


def test_invalid_late_stop_does_not_invent_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(breakeven_trigger_r=1)
    module.on_bar(
        view_factory(0, candle=candle_factory(0), position=position_factory(stop_loss=None))
    )
    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=104, high=106, low=103, close=105),
            position=position_factory(stop_loss=105),
        )
    )
    assert decision.position_updates == ()
    assert decision.skips == (PositionExitReason.MISSING_INITIAL_RISK.value,)
