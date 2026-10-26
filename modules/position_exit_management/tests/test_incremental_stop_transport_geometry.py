from __future__ import annotations

from modular_strategy_position_exit_management import PositionExitManagementModule


def test_subnormal_stop_that_collapses_to_market_is_not_emitted(
    view_factory, candle_factory, position_factory
) -> None:
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.1",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    minimum_positive_float = 5e-324
    position = position_factory(
        entry_price=minimum_positive_float,
        stop_loss=None,
        take_profit=None,
    )
    candle = candle_factory(
        0,
        open_=minimum_positive_float,
        high=minimum_positive_float,
        low=minimum_positive_float,
        close=minimum_positive_float,
    )

    decision = module.on_bar(view_factory(0, candle=candle, position=position))

    assert decision.position_updates == ()
    active = module.snapshot_state()["active_position"]
    assert active["trailing_active"] is False
    assert active["last_stop"] is None
