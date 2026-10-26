from __future__ import annotations

import pytest

from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitReason,
)


def _activate_partial(module, view_factory, candle_factory, position):
    return module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=106, low=99, close=105),
            position=position,
        )
    )


def test_pending_partial_retry_is_not_blocked_by_data_gap(
    view_factory, candle_factory, position_factory
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    position = position_factory(quantity=4, stop_loss=95)
    activated = _activate_partial(module, view_factory, candle_factory, position)
    assert activated.closes[0].quantity == pytest.approx(2)

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=200, low=1, close=105),
            position=position,
            data_gap=True,
        )
    )

    assert retry.closes[0].quantity == pytest.approx(2)
    assert retry.features[0].value["reason"] == PositionExitReason.PARTIAL_EXIT_RETRY.value
    assert retry.skips == (PositionExitReason.DATA_GAP.value,)
    assert module.snapshot_state()["active_position"]["favorable_extreme"] == "106"


def test_exit_pending_keeps_priority_over_gap_partial_retry(
    view_factory, candle_factory, position_factory
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    position = position_factory(quantity=4, stop_loss=95)
    _activate_partial(module, view_factory, candle_factory, position)

    decision = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=200, low=1, close=105),
            position=position,
            data_gap=True,
            exit_pending=True,
        )
    )

    assert decision.closes == ()
    assert decision.skips == (PositionExitReason.CLOSE_PENDING.value,)
    assert module.snapshot_state()["active_position"]["favorable_extreme"] == "106"
