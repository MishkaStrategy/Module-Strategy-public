from __future__ import annotations

import pytest

from modular_core import ModuleManifest
from modular_strategy_position_exit_management import (
    PositionExitManagementModule,
)

_ALLOWED_PARAMETER_RULES = {
    "type",
    "required",
    "minimum",
    "maximum",
    "enum",
    "default",
}


def test_position_exit_manifest_imports_on_frozen_core_0_4_2() -> None:
    manifest = PositionExitManagementModule.manifest
    assert isinstance(manifest, ModuleManifest)
    unsupported = {
        key
        for rules in manifest.parameters_schema.values()
        for key in rules
        if key not in _ALLOWED_PARAMETER_RULES
    }
    assert unsupported == set()
    assert manifest.parameters_schema["partial_exits"] == {
        "type": "array",
        "default": (),
    }
    module = PositionExitManagementModule(
        breakeven_trigger_r="0",
        trailing_distance_r="1",
        trailing_distance_pct="0.01",
    )
    assert module.manifest is manifest


@pytest.mark.parametrize(
    ("name", "value", "message"),
    [
        ("breakeven_trigger_r", "-0.1", ">= 0"),
        ("trailing_distance_r", "0", "> 0"),
        ("trailing_distance_pct", "1", "< 1"),
        ("market_buffer_bps", "10000", "< 10000"),
    ],
)
def test_position_exit_decimal_bounds_remain_fail_closed(
    name: str,
    value: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        PositionExitManagementModule(**{name: value})


def test_position_exit_partial_exit_limit_remains_fail_closed() -> None:
    rules = [
        {
            "trigger_r": str(index),
            "fraction": "0.01",
        }
        for index in range(65)
    ]
    with pytest.raises(ValueError, match="cannot contain more than 64 rules"):
        PositionExitManagementModule(partial_exits=rules)
