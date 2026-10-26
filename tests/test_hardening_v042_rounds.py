from __future__ import annotations

import math

import pytest

from modular_core import DataConfig, EngineConfig, ExecutionConfig
from modular_core.validation import finite_number, nonnegative_number, positive_number


@pytest.mark.parametrize("function", [finite_number, positive_number, nonnegative_number])
def test_round01_numeric_boundaries_reject_bool(function) -> None:
    with pytest.raises(TypeError, match="must be numeric"):
        function(True, "value")


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_round01_numeric_boundaries_reject_nonfinite(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        finite_number(value, "value")


@pytest.mark.parametrize(
    ("factory", "kwargs", "message"),
    [
        (
            ExecutionConfig,
            {"allow_exit_on_entry_bar": 1},
            "allow_exit_on_entry_bar must be bool",
        ),
        (
            ExecutionConfig,
            {"allow_reentry_on_exit_bar": 0},
            "allow_reentry_on_exit_bar must be bool",
        ),
        (
            DataConfig,
            {"expected_interval_seconds": 60.5},
            "expected_interval_seconds must be int",
        ),
        (
            DataConfig,
            {"expected_interval_seconds": True},
            "expected_interval_seconds must be int",
        ),
        (DataConfig, {"max_base_history": 5.5}, "max_base_history must be int"),
        (
            EngineConfig,
            {"strict_module_errors": 1},
            "strict_module_errors must be bool",
        ),
    ],
)
def test_round02_config_fields_reject_runtime_type_coercion(factory, kwargs, message: str) -> None:
    with pytest.raises(TypeError, match=message):
        factory(**kwargs)
