from datetime import datetime, timezone

import pytest

from modular_core import Candle, EngineConfig
from modular_core.run_manifest import build_run_manifest
from modular_core.serialization import deep_freeze, to_jsonable

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
BAR = Candle(NOW, 100, 101, 99, 100, 1, "X", "5m")


@pytest.mark.parametrize("operation", [deep_freeze, to_jsonable])
def test_serialization_rejects_non_string_mapping_keys(operation) -> None:
    with pytest.raises(TypeError, match="keys must be str"):
        operation({1: "integer", "1": "string"})


def test_run_identity_rejects_non_string_config_keys() -> None:
    with pytest.raises(TypeError, match="mapping keys must be str"):
        build_run_manifest(
            kernel_version="0.4.2",
            created_at=NOW,
            bars=(BAR,),
            config={1: "ambiguous"},
            manifests=(),
        )


@pytest.mark.parametrize("value", [float("inf"), float("-inf"), float("nan")])
def test_non_finite_json_values_fail_instead_of_becoming_strings(value: float) -> None:
    with pytest.raises(ValueError, match="finite"):
        to_jsonable(value)
    assert to_jsonable(str(value)) == str(value)


def test_set_serialization_is_canonical_within_supported_value_types() -> None:
    assert to_jsonable({3, 1, 2}) == [1, 2, 3]
    assert to_jsonable({"b", "a", "c"}) == ["a", "b", "c"]


def test_unsupported_values_fail_at_serialization_boundary() -> None:
    class Unsupported:
        pass

    with pytest.raises(TypeError, match="Unsupported"):
        to_jsonable(Unsupported())
    with pytest.raises(TypeError, match="Unsupported"):
        build_run_manifest(
            kernel_version="0.4.2",
            created_at=NOW,
            bars=(BAR,),
            config={"value": Unsupported()},
            manifests=(),
        )


def test_normal_string_keyed_payloads_remain_stable() -> None:
    payload = {"outer": {"inner": [1, 2, 3]}}
    assert to_jsonable(deep_freeze(payload)) == payload
    first = build_run_manifest(
        kernel_version="0.4.2",
        created_at=NOW,
        bars=(BAR,),
        config=EngineConfig(),
        manifests=(),
    )
    second = build_run_manifest(
        kernel_version="0.4.2",
        created_at=NOW,
        bars=(BAR,),
        config=EngineConfig(),
        manifests=(),
    )
    assert first.run_id == second.run_id
