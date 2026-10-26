from __future__ import annotations

from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
    PositionExitReason,
)


def test_future_position_does_not_override_out_of_order_rejection(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    module.on_bar(
        view_factory(
            1,
            candle=candle_factory(1),
            position=position_factory(),
        )
    )
    before = module.snapshot_state()

    rejected = module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(entry_minutes=30),
        )
    )

    assert rejected.skips == (PositionExitReason.OUT_OF_ORDER_EVENT.value,)
    assert module.snapshot_state() == before


def test_future_position_does_not_override_conflicting_duplicate_rejection(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    candle = candle_factory(1)
    module.on_bar(view_factory(1, candle=candle, position=position_factory()))
    before = module.snapshot_state()

    rejected = module.on_bar(
        view_factory(
            1,
            candle=candle,
            position=position_factory(entry_minutes=30),
        )
    )

    assert rejected.skips == (PositionExitReason.CONFLICTING_DUPLICATE.value,)
    assert module.snapshot_state() == before
