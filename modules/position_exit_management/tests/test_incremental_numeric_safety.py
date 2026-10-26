from __future__ import annotations

import json
from dataclasses import replace
from decimal import Decimal

import pytest
from modular_core import PositionSide

from modular_strategy_position_exit_management import PositionExitManagementModule


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("quantity", Decimal("1e10000")),
        ("quantity", Decimal("1e-10000")),
        ("entry_price", 10**10000),
        ("stop_loss", Decimal("1e10000")),
    ],
    ids=(
        "huge-decimal-quantity",
        "tiny-decimal-quantity",
        "huge-integer-entry-price",
        "huge-decimal-stop-loss",
    ),
)
def test_position_values_must_fit_public_float_transport_before_state_mutation(
    view_factory,
    position_factory,
    field,
    value,
):
    module = PositionExitManagementModule()
    malformed = replace(position_factory(), **{field: value})
    before = module.snapshot_state()

    with pytest.raises(ValueError, match=f"position.{field} must fit"):
        module.on_bar(view_factory(position=malformed))

    assert module.snapshot_state() == before


def test_unrepresentable_short_stop_candidate_is_not_emitted(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        trailing_mode="percent",
        trailing_activation_r=0,
        trailing_distance_pct="0.99",
        minimum_stop_move_bps=0,
        market_buffer_bps=0,
    )
    position = position_factory(
        side=PositionSide.SHORT,
        entry_price=1e308,
        stop_loss=None,
        take_profit=None,
    )
    candle = candle_factory(
        0,
        open_=1e308,
        high=1.1e308,
        low=1e308,
        close=1e308,
    )

    decision = module.on_bar(view_factory(0, candle=candle, position=position))

    assert decision.position_updates == ()
    assert module.snapshot_state()["active_position"]["trailing_active"] is False


def test_extreme_r_diagnostics_remain_json_safe_and_exact(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(emit_status_feature=True)
    position = position_factory(
        entry_price=1e-300,
        stop_loss=5e-324,
        take_profit=None,
    )
    candle = candle_factory(
        0,
        open_=1e-300,
        high=1e308,
        low=5e-324,
        close=1e308,
    )

    decision = module.on_bar(view_factory(0, candle=candle, position=position))

    current_r = decision.diagnostics["current_r"]
    assert isinstance(current_r, str)
    assert current_r not in {"inf", "-inf", "nan"}
    assert decision.features[0].value["diagnostics"]["current_r"] == current_r
    json.dumps(dict(decision.diagnostics), allow_nan=False)
