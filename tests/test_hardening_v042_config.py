from __future__ import annotations

from datetime import datetime, timezone
import math

import pytest

from modular_core import Candle, ModuleManifest
from modular_core.run_manifest import build_run_manifest

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BARS = (Candle(NOW, 100, 101, 99, 100, 1, "X", "5m"),)


def _manifest(config: object):
    return build_run_manifest(
        kernel_version="0.4.2",
        created_at=NOW,
        bars=BARS,
        config=config,
        manifests=(ModuleManifest(name="probe"),),
    )


def test_round09_config_fingerprint_is_mapping_order_and_signed_zero_invariant() -> None:
    first = _manifest({"outer": {"a": -0.0, "b": 0.0}, "enabled": True})
    second = _manifest({"enabled": True, "outer": {"b": -0.0, "a": 0.0}})
    assert first.config_hash == second.config_hash
    assert first.run_id == second.run_id


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_round09_config_fingerprint_rejects_nonfinite_nested_values(
    value: float,
) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        _manifest({"nested": {"threshold": value}})


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"": {"type": "number"}}, "parameter name"),
        ({"value": {"type": "mystery"}}, "unsupported parameter type"),
        ({"value": {"type": "number", "required": 1}}, "required must be bool"),
        (
            {"value": {"type": "number", "minimum": 2, "maximum": 1}},
            "minimum cannot exceed maximum",
        ),
        (
            {"value": {"type": "string", "enum": ["a", "a"]}},
            "enum cannot contain duplicates",
        ),
        ({"value": {"type": "integer", "default": 1.5}}, "default must be integer"),
        (
            {"value": {"type": "number", "minimum": 2, "default": 1}},
            "default below minimum",
        ),
        (
            {"value": {"type": "string", "enum": ["a", "b"], "default": "c"}},
            "default must be one of",
        ),
    ],
)
def test_round10_parameter_schema_fails_closed(schema: object, message: str) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        ModuleManifest(name="probe", parameters_schema=schema)  # type: ignore[arg-type]
