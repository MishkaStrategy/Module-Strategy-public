from __future__ import annotations

import pytest

from modular_strategy_position_exit_management import PositionExitManagementModule


class _NumericStringImpostor:
    def __init__(self) -> None:
        self.stringification_attempted = False

    def __str__(self) -> str:
        self.stringification_attempted = True
        return "1"


def test_decimal_config_rejects_non_contract_object_before_stringification() -> None:
    impostor = _NumericStringImpostor()

    with pytest.raises(TypeError, match="breakeven_trigger_r must be numeric"):
        PositionExitManagementModule(breakeven_trigger_r=impostor)  # type: ignore[arg-type]

    assert impostor.stringification_attempted is False
