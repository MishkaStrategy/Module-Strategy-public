from __future__ import annotations

from decimal import Decimal

import pytest

from modular_strategy_fvg import FvgConfig, FvgModule


def test_defaults_are_canonical_and_deeply_immutable() -> None:
    config = FvgConfig()
    assert config.timeframes == ("15m", "4h")
    assert config.max_active_zones_per_timeframe == 4096
    assert config.max_state_bytes == 8 * 1024 * 1024
    assert isinstance(config.as_parameters()["timeframes"], tuple)
    with pytest.raises(TypeError):
        config.as_parameters()["x"] = 1  # type: ignore[index]


@pytest.mark.parametrize("value", ["15m", b"15m"])
def test_timeframes_reject_scalar_strings(value: object) -> None:
    with pytest.raises(TypeError):
        FvgConfig.from_parameters(timeframes=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [(), [], [""], ["0m"], ["15x"], ["-1h"], ["15m", "15M"]])
def test_invalid_timeframe_sets(value: object) -> None:
    with pytest.raises(ValueError):
        FvgConfig.from_parameters(timeframes=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["require_consecutive_bars", "publish_lifecycle_events"])
@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_bool_fields_are_strict(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        FvgConfig.from_parameters(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["max_active_zones_per_timeframe", "max_state_bytes"])
@pytest.mark.parametrize("value", [True, False, 1.0, "1024", None])
def test_integer_fields_are_strict(field: str, value: object) -> None:
    with pytest.raises(TypeError):
        FvgConfig.from_parameters(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_active_zones_per_timeframe", 0),
        ("max_active_zones_per_timeframe", 100_001),
        ("max_state_bytes", 1023),
        ("max_state_bytes", 64 * 1024 * 1024 + 1),
    ],
)
def test_integer_bounds(field: str, value: int) -> None:
    with pytest.raises(ValueError):
        FvgConfig.from_parameters(**{field: value})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", float("nan"), float("inf")])
def test_nonfinite_decimal_config_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        FvgConfig.from_parameters(min_gap_absolute=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["-0.1", -1, Decimal("-1e-30")])
def test_negative_absolute_gap_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        FvgConfig.from_parameters(min_gap_absolute=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["-0.1", "1", "1.0001"])
def test_fraction_bounds_rejected(value: str) -> None:
    with pytest.raises(ValueError):
        FvgConfig.from_parameters(min_gap_fraction=value)


def test_manifest_declares_hardening_parameters() -> None:
    module = FvgModule()
    assert module.manifest.version == "0.3.0"
    assert module.manifest.state_schema_version == "4"
    assert "max_active_zones_per_timeframe" in module.manifest.parameters_schema
    assert "max_state_bytes" in module.manifest.parameters_schema
    assert "gap_size_mode" in module.manifest.parameters_schema
    assert "pre_fvg_enabled" in module.manifest.parameters_schema


def test_semantically_equal_decimal_parameters_have_same_fingerprint() -> None:
    left = FvgModule(timeframes=("15m",), min_gap_absolute="0", min_gap_fraction="0.0100")
    right = FvgModule(timeframes=("15m",), min_gap_absolute="0.00", min_gap_fraction="0.01")
    assert left.snapshot_state()["config_fingerprint"] == right.snapshot_state()["config_fingerprint"]
