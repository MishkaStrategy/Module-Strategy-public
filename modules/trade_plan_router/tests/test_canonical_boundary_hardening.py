from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

import pytest

from modular_strategy_trade_plan_router import canonical


class Choice(Enum):
    FIRST = "first"


@dataclass
class Sample:
    public: int
    _private: int


class DuplicateMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        if key == "dup":
            return 1
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        yield "dup"

    def __len__(self) -> int:
        return 1

    def items(self):
        return (("dup", 1), ("dup", 2))


def test_decimal_text_covers_sign_scale_and_trimming() -> None:
    assert canonical.decimal_text(Decimal("0E-10")) == "0"
    assert canonical.decimal_text(Decimal("1E+3")) == "1000"
    assert canonical.decimal_text(Decimal("-12.3400")) == "-12.34"
    assert canonical.decimal_text(Decimal("0.00120")) == "0.0012"
    assert canonical.decimal_text(Decimal("100.00")) == "100"


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity")))
def test_decimal_text_rejects_non_finite(value: Decimal) -> None:
    with pytest.raises(ValueError, match="non-finite"):
        canonical.decimal_text(value)


def test_decimal_text_limits_all_exponent_geometries(monkeypatch) -> None:
    monkeypatch.setattr(canonical, "MAX_CANONICAL_DECIMAL_TEXT_LENGTH", 3)
    with pytest.raises(ValueError, match="maximum text length"):
        canonical.decimal_text(Decimal("1E+3"))
    with pytest.raises(ValueError, match="maximum text length"):
        canonical.decimal_text(Decimal("12.3"))
    with pytest.raises(ValueError, match="maximum text length"):
        canonical.decimal_text(Decimal("0.001"))


def test_utc_text_normalizes_offsets_and_rejects_naive() -> None:
    value = datetime(2026, 1, 1, 3, 0, tzinfo=UTC) + timedelta(hours=1)
    assert canonical.utc_text(value).endswith("Z")
    with pytest.raises(ValueError, match="timezone-aware"):
        canonical.utc_text(datetime(2026, 1, 1))


def test_control_and_unicode_string_boundaries(monkeypatch) -> None:
    assert canonical.has_control_characters("") is False
    assert canonical.has_control_characters("visible") is False
    assert canonical.has_control_characters("line\nbreak") is True
    with pytest.raises(ValueError, match="control characters"):
        canonical.canonical_value("line\nbreak")
    with pytest.raises(ValueError, match="NFC"):
        canonical.canonical_value("e\u0301")
    monkeypatch.setattr(canonical, "MAX_CANONICAL_STRING_LENGTH", 3)
    with pytest.raises(ValueError, match="maximum length"):
        canonical.canonical_value("long")


def test_canonical_value_normalizes_supported_composites() -> None:
    value = {
        "enum": Choice.FIRST,
        "decimal": Decimal("1.20"),
        "time": datetime(2026, 1, 1, tzinfo=UTC),
        "dataclass": Sample(2, 99),
        "set": {"b", "a"},
        "sequence": (1, True, None),
        "float": 1.5,
    }
    normalized = canonical.canonical_value(value)
    assert normalized == {
        "dataclass": {"public": 2},
        "decimal": "1.2",
        "enum": "first",
        "float": 1.5,
        "sequence": [1, True, None],
        "set": ["a", "b"],
        "time": "2026-01-01T00:00:00Z",
    }
    assert canonical.canonical_json({"b": 2, "a": 1}) == '{"a":1,"b":2}'


@pytest.mark.parametrize("value", (float("nan"), float("inf"), float("-inf")))
def test_canonical_value_rejects_non_finite_float(value: float) -> None:
    with pytest.raises(ValueError, match="non-finite float"):
        canonical.canonical_value(value)


def test_canonical_value_rejects_invalid_mappings_and_types() -> None:
    with pytest.raises(ValueError, match="mapping keys must be strings"):
        canonical.canonical_value({1: "value"})
    with pytest.raises(ValueError, match="duplicate mapping key"):
        canonical.canonical_value(DuplicateMapping())
    with pytest.raises(TypeError, match="unsupported canonical value"):
        canonical.canonical_value(object())
    with pytest.raises(TypeError, match="unsupported canonical value"):
        canonical.canonical_value(Sample)


def test_canonical_integer_boundaries(monkeypatch) -> None:
    monkeypatch.setattr(canonical, "MIN_CANONICAL_INTEGER", -2)
    monkeypatch.setattr(canonical, "MAX_CANONICAL_INTEGER", 2)
    assert canonical.canonical_value(-2) == -2
    assert canonical.canonical_value(2) == 2
    with pytest.raises(ValueError, match="signed 64-bit range"):
        canonical.canonical_value(3)


def test_canonical_cycle_depth_and_node_limits(monkeypatch) -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cyclic canonical value"):
        canonical.canonical_value(cyclic)

    monkeypatch.setattr(canonical, "MAX_CANONICAL_DEPTH", 1)
    with pytest.raises(ValueError, match="maximum depth"):
        canonical.canonical_value([[[1]]])

    monkeypatch.setattr(canonical, "MAX_CANONICAL_DEPTH", 64)
    monkeypatch.setattr(canonical, "MAX_CANONICAL_NODES", 2)
    with pytest.raises(ValueError, match="maximum node count"):
        canonical.canonical_value([1, 2])


def test_hash_and_stable_id_contracts() -> None:
    first = canonical.sha256_hex({"a": 1, "b": 2})
    second = canonical.sha256_hex({"b": 2, "a": 1})
    assert first == second
    assert len(first) == 64
    assert canonical.stable_id("trade_plan", {"a": 1}, length=8).startswith("trade_plan-")
    for prefix in ("", "bad prefix", "!"):
        with pytest.raises(ValueError, match="identifier-like"):
            canonical.stable_id(prefix, {})
    for length in (7, 65):
        with pytest.raises(ValueError, match="hash length"):
            canonical.stable_id("valid", {}, length=length)


def test_deep_freeze_and_thaw_round_trip_supported_values() -> None:
    source = {
        "enum": Choice.FIRST,
        "list": [1, Decimal("1.2")],
        "tuple": (datetime(2026, 1, 1, tzinfo=UTC),),
        "set": {"b", "a"},
        "mapping": {"nested": True},
    }
    frozen = canonical.deep_freeze(source)
    assert isinstance(frozen, MappingProxyType)
    assert frozen["enum"] is Choice.FIRST
    assert frozen["list"] == (1, Decimal("1.2"))
    assert frozen["set"] == frozenset({"a", "b"})
    thawed = canonical.deep_thaw(frozen)
    assert thawed["enum"] is Choice.FIRST
    assert thawed["list"] == [1, Decimal("1.2")]
    assert thawed["set"] == ["a", "b"]


@pytest.mark.parametrize("function", (canonical.deep_freeze, canonical.deep_thaw))
def test_deep_mapping_rejects_invalid_scalars(function) -> None:
    with pytest.raises(ValueError, match="non-finite float"):
        function({"value": float("nan")})
    with pytest.raises(ValueError, match="signed 64-bit range"):
        function({"value": canonical.MAX_CANONICAL_INTEGER + 1})
    with pytest.raises(ValueError, match="timezone-aware"):
        function({"value": datetime(2026, 1, 1)})
    with pytest.raises(TypeError, match="unsupported mapping value"):
        function({"value": object()})


@pytest.mark.parametrize("function", (canonical.deep_freeze, canonical.deep_thaw))
def test_deep_mapping_cycle_depth_and_node_limits(function, monkeypatch) -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)
    with pytest.raises(ValueError, match="cyclic canonical value"):
        function(cyclic)

    monkeypatch.setattr(canonical, "MAX_CANONICAL_DEPTH", 1)
    with pytest.raises(ValueError, match="maximum depth"):
        function([[[1]]])

    monkeypatch.setattr(canonical, "MAX_CANONICAL_DEPTH", 64)
    monkeypatch.setattr(canonical, "MAX_CANONICAL_NODES", 2)
    with pytest.raises(ValueError, match="maximum node count"):
        function([1, 2])


def test_deep_mapping_validates_mapping_keys() -> None:
    with pytest.raises(ValueError, match="mapping keys must be strings"):
        canonical.deep_freeze({1: "value"})
    with pytest.raises(ValueError, match="duplicate mapping key"):
        canonical.deep_thaw(DuplicateMapping())
