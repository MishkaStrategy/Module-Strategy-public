from __future__ import annotations

import pytest

from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitReason,
)


def test_tiny_partial_exit_is_emitted_and_retried_at_float_resolution(
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    position = position_factory(quantity=1e-13)

    first = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=105, high=106, low=104, close=105),
            position=position,
        )
    )
    assert first.closes
    assert first.closes[0].quantity == pytest.approx(5e-14)
    assert first.closes[0].reason.endswith("stage_1")

    retry = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=106, low=104, close=105),
            position=position,
        )
    )
    assert retry.closes
    assert retry.closes[0].quantity == pytest.approx(5e-14)
    assert retry.skips == ()
    assert retry.features[0].value["reason"] == PositionExitReason.PARTIAL_EXIT_RETRY.value
