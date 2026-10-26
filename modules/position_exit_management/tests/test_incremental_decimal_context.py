from __future__ import annotations

from decimal import localcontext

import pytest

from modular_strategy_position_exit_management import PositionExitManagementModule


def _run_under_precision(
    precision,
    *,
    view_factory,
    candle_factory,
    position_factory,
):
    with localcontext() as context:
        context.prec = precision
        module = PositionExitManagementModule(
            trailing_mode="r_multiple",
            trailing_activation_r=0,
            trailing_distance_r=("0.333333333333333333333333333333333333333333333333333333333333"),
            minimum_stop_move_bps=0,
            market_buffer_bps=0,
            emit_status_feature=True,
        )
        decision = module.on_bar(
            view_factory(
                0,
                candle=candle_factory(0, open_=109, high=110, low=108, close=109),
                position=position_factory(stop_loss=95),
            )
        )
        return decision, module.snapshot_state(), module.config.fingerprint


def test_event_processing_is_independent_of_ambient_decimal_context(
    view_factory,
    candle_factory,
    position_factory,
):
    low = _run_under_precision(
        6,
        view_factory=view_factory,
        candle_factory=candle_factory,
        position_factory=position_factory,
    )
    high = _run_under_precision(
        120,
        view_factory=view_factory,
        candle_factory=candle_factory,
        position_factory=position_factory,
    )

    assert low == high


def test_partial_fraction_validation_is_independent_of_ambient_decimal_context():
    parameters = {
        "partial_exits": [
            {"trigger_r": 1, "fraction": "0.5000000000000000000000000001"},
            {"trigger_r": 2, "fraction": "0.5"},
        ]
    }

    for precision in (6, 28, 120):
        with localcontext() as context:
            context.prec = precision
            with pytest.raises(ValueError, match="fractions cannot exceed 1"):
                PositionExitManagementModule(**parameters)


def test_partial_fraction_validation_preserves_full_supported_exponent_range():
    parameters = {
        "partial_exits": [
            {"trigger_r": 1, "fraction": "1"},
            {"trigger_r": 2, "fraction": "1e-4096"},
        ]
    }

    with localcontext() as context:
        context.prec = 6
        with pytest.raises(ValueError, match="fractions cannot exceed 1"):
            PositionExitManagementModule(**parameters)


def test_checkpoint_roundtrip_is_independent_of_ambient_decimal_context(
    view_factory,
    candle_factory,
    position_factory,
):
    source = PositionExitManagementModule(
        trailing_mode="r_multiple",
        trailing_activation_r=0,
        trailing_distance_r="0.123456789012345678901234567890123456789",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    source.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=109, high=110, low=108, close=109),
            position=position_factory(stop_loss=95),
        )
    )
    checkpoint = source.snapshot_state()

    with localcontext() as context:
        context.prec = 5
        restored = PositionExitManagementModule(
            trailing_mode="r_multiple",
            trailing_activation_r=0,
            trailing_distance_r="0.123456789012345678901234567890123456789",
            minimum_stop_move_bps=0,
            market_buffer_bps=0,
        )
        restored.restore_state(checkpoint)
        roundtrip = restored.snapshot_state()

    assert roundtrip == checkpoint
