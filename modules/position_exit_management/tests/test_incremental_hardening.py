from __future__ import annotations

from copy import deepcopy

import pytest

from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitReason,
)


def test_future_position_rejection_is_atomic_and_same_bar_is_recoverable(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(trailing_mode="percent", trailing_activation_r=0)
    candle = candle_factory(0, open_=150, high=200, low=140, close=180)
    before = module.snapshot_state()

    rejected = module.on_bar(
        view_factory(
            0,
            candle=candle,
            position=position_factory(entry_minutes=30),
        )
    )

    assert rejected.skips == (PositionExitReason.FUTURE_ENTRY_TIME.value,)
    assert module.snapshot_state() == before

    accepted = module.on_bar(
        view_factory(
            0,
            candle=candle,
            position=position_factory(entry_minutes=0),
        )
    )
    assert PositionExitReason.CONFLICTING_DUPLICATE.value not in accepted.skips
    assert accepted.diagnostics["bars_managed"] == 1


def test_restore_rejects_completed_partial_without_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    parameters = {"partial_exits": [{"trigger_r": 1, "fraction": "0.5"}]}
    source = PositionExitManagementModule(**parameters)
    source.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(stop_loss=None),
        )
    )
    broken = deepcopy(source.snapshot_state())
    broken["active_position"]["completed_partial_indices"] = (0,)

    target = PositionExitManagementModule(**parameters)
    with pytest.raises(ValueError, match="partial-exit state requires initial risk"):
        target.restore_state(broken)
    assert target.snapshot_state()["active_position"] is None


def test_restore_rejects_pending_partial_without_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    parameters = {"partial_exits": [{"trigger_r": 1, "fraction": "0.5"}]}
    source = PositionExitManagementModule(**parameters)
    source.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(stop_loss=None, quantity=4),
        )
    )
    broken = deepcopy(source.snapshot_state())
    broken["active_position"]["pending_partial_index"] = 0
    broken["active_position"]["pending_partial_target_quantity"] = "2"

    target = PositionExitManagementModule(**parameters)
    with pytest.raises(ValueError, match="partial-exit state requires initial risk"):
        target.restore_state(broken)
    assert target.snapshot_state()["active_position"] is None


def test_restore_rejects_r_activated_percent_trailing_without_initial_risk(
    view_factory,
    candle_factory,
    position_factory,
):
    parameters = {
        "trailing_mode": "percent",
        "trailing_activation_r": 1,
        "trailing_distance_pct": "0.1",
    }
    source = PositionExitManagementModule(**parameters)
    source.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(stop_loss=None),
        )
    )
    broken = deepcopy(source.snapshot_state())
    broken["active_position"]["trailing_active"] = True

    target = PositionExitManagementModule(**parameters)
    with pytest.raises(
        ValueError, match="R-activated percent trailing state requires initial risk"
    ):
        target.restore_state(broken)
    assert target.snapshot_state()["active_position"] is None


def test_entry_bar_extreme_cannot_trigger_partial_exit_from_pre_fill_prices(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": "0.5"}],
        trigger_source="extreme",
    )
    position = position_factory(entry_minutes=3, stop_loss=95, quantity=4)

    entry_bar = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=110, low=99, close=101),
            position=position,
        )
    )
    assert entry_bar.closes == ()
    assert module.snapshot_state()["active_position"]["favorable_extreme"] == "101"

    complete_post_entry_bar = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=101, high=106, low=100, close=101),
            position=position,
        )
    )
    assert complete_post_entry_bar.closes[0].quantity == pytest.approx(2)


def test_entry_bar_percent_trailing_ignores_pre_fill_extreme(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.1",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )

    decision = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=200, low=99, close=101),
            position=position_factory(entry_minutes=3, stop_loss=95),
        )
    )

    assert decision.position_updates == ()
    assert module.snapshot_state()["active_position"]["favorable_extreme"] == "101"
