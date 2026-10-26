from __future__ import annotations

from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitReason,
)


def test_partial_confirmation_emits_typed_status_without_repeating_close(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": "0.5"}])
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(quantity=4),
        )
    )

    confirmed = module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1, open_=105, high=106, low=104, close=105),
            position=position_factory(quantity=2),
        )
    )

    assert confirmed.closes == ()
    assert confirmed.position_updates == ()
    assert confirmed.skips == ()
    assert confirmed.diagnostics["partial_completed_index"] == 0
    assert len(confirmed.features) == 1
    assert confirmed.features[0].value["reason"] == PositionExitReason.PARTIAL_EXIT_CONFIRMED.value
    assert confirmed.features[0].value["completed_partial_indices"] == (0,)
