from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, localcontext

import pytest

from modular_strategy_position_exit_management import (
    PartialExitRule,
    PositionExitConfig,
    PositionExitManagementModule,
)


@pytest.mark.parametrize(
    "parameters",
    [
        {"breakeven_trigger_r": "1e4097"},
        {"breakeven_offset_r": "1e-4097"},
        {"partial_exits": [{"trigger_r": "1e4097", "fraction": "0.5"}]},
        {"minimum_stop_move_bps": "1" * 257},
    ],
)
def test_configuration_rejects_resource_unbounded_decimal_values(parameters):
    with pytest.raises(ValueError, match="supported Decimal precision or exponent range"):
        PositionExitManagementModule(**parameters)


def test_partial_rule_construction_applies_decimal_bounds():
    with pytest.raises(ValueError, match="partial exit fraction"):
        PartialExitRule(trigger_r=Decimal("1"), fraction=Decimal("1e-4097"))


def test_config_fingerprint_is_exact_independently_of_decimal_context():
    first_value = "1." + ("0" * 80) + "1"
    second_value = "1." + ("0" * 80) + "2"

    with localcontext() as context:
        context.prec = 8
        first = PositionExitConfig.from_parameters(breakeven_trigger_r=first_value)
        second = PositionExitConfig.from_parameters(breakeven_trigger_r=second_value)

    assert first.canonical_mapping()["breakeven_trigger_r"] == first_value
    assert second.canonical_mapping()["breakeven_trigger_r"] == second_value
    assert first.fingerprint != second.fingerprint


def test_restore_rejects_resource_unbounded_decimal_state_atomically(
    view_factory,
    candle_factory,
    position_factory,
):
    source = PositionExitManagementModule()
    source.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(),
        )
    )
    broken = deepcopy(source.snapshot_state())
    broken["active_position"]["initial_quantity"] = "1e4097"
    broken["active_position"]["last_seen_quantity"] = "1e4097"

    target = PositionExitManagementModule()
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="initial_quantity"):
        target.restore_state(broken)
    assert target.snapshot_state() == before
