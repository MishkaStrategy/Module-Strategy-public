from __future__ import annotations

from copy import deepcopy

import pytest

import modular_strategy_position_exit_management.state as state_module
from modular_strategy_position_exit_management import PositionExitManagementModule
from modular_strategy_position_exit_management.decimal_utils import (
    MAX_DECIMAL_TEXT_CHARS,
)


class _ExplosiveNumericText(str):
    def __str__(self) -> str:
        raise AssertionError("oversized config text must be rejected before conversion")


def test_config_rejects_oversized_decimal_text_before_conversion() -> None:
    value = _ExplosiveNumericText("1" * (MAX_DECIMAL_TEXT_CHARS + 1))

    with pytest.raises(ValueError, match="Decimal text length"):
        PositionExitManagementModule(breakeven_trigger_r=value)


def test_restore_rejects_oversized_decimal_text_before_parsing_atomically(
    monkeypatch,
    view_factory,
    candle_factory,
    position_factory,
) -> None:
    module = PositionExitManagementModule()
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(),
        )
    )
    original = module.snapshot_state()
    broken = deepcopy(original)
    broken["active_position"]["entry_price"] = "1" * (MAX_DECIMAL_TEXT_CHARS + 1)

    def _unexpected_decimal_parse(value):
        raise AssertionError(f"unexpected Decimal parse for {type(value).__name__}")

    monkeypatch.setattr(state_module, "Decimal", _unexpected_decimal_parse)
    with pytest.raises(ValueError, match="Decimal text length"):
        module.restore_state(broken)
    assert module.snapshot_state() == original
