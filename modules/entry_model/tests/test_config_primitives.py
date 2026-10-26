from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

import pytest
from modular_core import Side

from modular_modules.entry_model import EntryKind, EntryModelConfig, SetupPayloadError
from modular_modules.entry_model.primitives import (
    canonical_json,
    decimal_text,
    json_safe_mapping,
    json_safe_value,
    optional_decimal,
    optional_decimal_text,
    parse_datetime,
    parse_decimal,
    parse_entry_kind,
    parse_side,
    primitive_json,
    required_text,
    sha256_hex,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("buy", Side.BUY),
        (" SELL ", Side.SELL),
        (Side.BUY, Side.BUY),
    ],
)
def test_parse_side(value: object, expected: Side) -> None:
    assert parse_side(value) is expected


@pytest.mark.parametrize("value", [None, "hold", 1, object()])
def test_parse_side_rejects(value: object) -> None:
    with pytest.raises(SetupPayloadError):
        parse_side(value)


@pytest.mark.parametrize("kind", list(EntryKind))
def test_parse_entry_kind_all(kind: EntryKind) -> None:
    assert parse_entry_kind(kind.value.upper()) is kind
    assert parse_entry_kind(kind) is kind


def test_parse_entry_kind_rejects() -> None:
    with pytest.raises(SetupPayloadError):
        parse_entry_kind("iceberg")


def test_parse_datetime_normalizes_to_utc() -> None:
    result = parse_datetime("2026-07-21T14:00:00+02:00", "ts")
    assert result == datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    assert parse_datetime("2026-07-21T12:00:00Z", "ts") == result


@pytest.mark.parametrize("value", [None, 123, "bad", datetime(2026, 7, 21)])
def test_parse_datetime_rejects_invalid(value: object) -> None:
    with pytest.raises(SetupPayloadError):
        parse_datetime(value, "ts")


@pytest.mark.parametrize(
    ("value", "positive", "expected"),
    [
        ("1.2300", True, Decimal("1.2300")),
        (0, False, Decimal("0")),
        (-1, False, Decimal("-1")),
        (Decimal("1E-1000"), True, Decimal("1E-1000")),
        (Decimal("1E+1000"), True, Decimal("1E+1000")),
    ],
)
def test_parse_decimal(value: object, positive: bool, expected: Decimal) -> None:
    assert parse_decimal(value, "price", positive=positive) == expected


@pytest.mark.parametrize("value", [None, True, "nan", "inf", "-inf", "bad", 0, -1])
def test_parse_decimal_rejects_positive_invalid(value: object) -> None:
    with pytest.raises(SetupPayloadError):
        parse_decimal(value, "price")


@pytest.mark.parametrize("value", [None, True, "nan", "inf", object()])
def test_parse_decimal_rejects_finite_invalid(value: object) -> None:
    with pytest.raises(SetupPayloadError):
        parse_decimal(value, "value", positive=False)


def test_optional_decimal_and_text() -> None:
    assert optional_decimal(None, "x") is None
    assert optional_decimal("1.20", "x") == Decimal("1.20")
    assert optional_decimal_text(None) is None
    assert optional_decimal_text(Decimal("1.2000")) == "1.2"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("0"), "0"),
        (Decimal("100.000"), "100"),
        (Decimal("0.0012300"), "0.00123"),
        (Decimal("1E+6"), "1000000"),
        (Decimal("1E-6"), "0.000001"),
    ],
)
def test_decimal_text(value: Decimal, expected: str) -> None:
    assert decimal_text(value) == expected


def test_decimal_text_preserves_precision_beyond_default_context() -> None:
    value = Decimal("1000000000.9183989289616175543")
    assert decimal_text(value) == "1000000000.9183989289616175543"


def test_decimal_text_rejects_nonfinite() -> None:
    with pytest.raises(ValueError):
        decimal_text(Decimal("NaN"))


@pytest.mark.parametrize("value", [None, "", "   ", False, 0])
def test_required_text_rejects(value: object) -> None:
    with pytest.raises(SetupPayloadError):
        required_text(value, "name")


def test_required_text_normalizes() -> None:
    assert required_text(" x ", "name") == "x"
    assert required_text(123, "name") == "123"


class Demo(Enum):
    VALUE = "demo"


def test_json_safe_nested_and_immutable() -> None:
    ts = datetime(2026, 7, 21, 12, 0, tzinfo=UTC)
    result = json_safe_mapping(
        {
            "none": None,
            "text": "x",
            "bool": True,
            "int": 1,
            "float": 1.5,
            "decimal": Decimal("1.20"),
            "datetime": ts,
            "enum": Demo.VALUE,
            "mapping": {"x": [1, Decimal("2.0")]},
        },
        "metadata",
    )
    assert isinstance(result, MappingProxyType)
    assert result["decimal"] == "1.2"
    assert result["datetime"] == ts.isoformat()
    assert result["enum"] == "demo"
    assert result["mapping"]["x"] == (1, "2")
    with pytest.raises(TypeError):
        result["x"] = 1  # type: ignore[index]


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), Decimal("NaN"), datetime(2026, 7, 21), {1: "x"}, {1, 2}, object()],
)
def test_json_safe_rejects(value: object) -> None:
    with pytest.raises(ValueError):
        json_safe_value(value, "metadata")


def test_primitive_json_and_canonical_hash_are_order_independent() -> None:
    left = {"b": (2, 3), "a": {"z": Decimal("1.0")}}
    right = {"a": {"z": Decimal("1")}, "b": [2, 3]}
    assert primitive_json((1, {"a": 2})) == [1, {"a": 2}]
    assert canonical_json(left) == canonical_json(right)
    assert sha256_hex(left) == sha256_hex(right)
    assert len(sha256_hex(left)) == 64


def test_config_defaults_and_manifest_safe_parameters() -> None:
    config = EntryModelConfig()
    assert config.default_entry_kind is EntryKind.LIMIT_RETRACEMENT
    assert config.retracement_fraction == Decimal("0.382")
    assert config.default_entry_kind in config.allowed_entry_kind_set
    assert len(config.fingerprint) == 64
    assert config.canonical_payload()["allowed_entry_kinds"] == tuple(
        sorted(kind.value for kind in EntryKind)
    )


def test_config_fingerprint_ignores_allowed_order_and_duplicates() -> None:
    a = EntryModelConfig.from_parameters(
        allowed_entry_kinds=(EntryKind.MARKET, EntryKind.LIMIT_RETRACEMENT, EntryKind.MARKET)
    )
    b = EntryModelConfig.from_parameters(
        allowed_entry_kinds=(EntryKind.LIMIT_RETRACEMENT, EntryKind.MARKET)
    )
    assert a.allowed_entry_kinds == (EntryKind.MARKET, EntryKind.LIMIT_RETRACEMENT)
    assert a.fingerprint == b.fingerprint


def test_config_parameter_export_preserves_decimal_identity() -> None:
    config = EntryModelConfig.from_parameters(
        retracement_fraction="0.123456789123456789",
        stop_limit_offset_bps="1.000000000000000001",
        max_setup_age_seconds="0.000000001",
    )
    params = config.as_parameters()
    assert params["retracement_fraction"] == "0.123456789123456789"
    assert params["stop_limit_offset_bps"] == "1.000000000000000001"
    assert params["max_setup_age_seconds"] == "0.000000001"


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"ttl_bars": True}, "ttl_bars"),
        ({"ttl_bars": 0}, "ttl_bars"),
        ({"ttl_bars": 1_000_001}, "ttl_bars"),
        ({"max_processed_setups": True}, "max_processed_setups"),
        ({"max_processed_setups": 0}, "max_processed_setups"),
        ({"max_processed_setups": 1_000_001}, "max_processed_setups"),
        ({"honor_setup_hint": 1}, "honor_setup_hint"),
        ({"post_only_limits": 1}, "post_only_limits"),
        ({"retracement_fraction": 0}, "retracement_fraction"),
        ({"retracement_fraction": 1}, "retracement_fraction"),
        ({"stop_limit_offset_bps": -1}, "stop_limit_offset_bps"),
        ({"stop_limit_offset_bps": 10_000}, "stop_limit_offset_bps"),
        ({"max_setup_age_seconds": -1}, "max_setup_age_seconds"),
        ({"allowed_entry_kinds": ()}, "allowed_entry_kinds"),
        (
            {"default_entry_kind": "market", "allowed_entry_kinds": ("limit_retracement",)},
            "default_entry_kind",
        ),
    ],
)
def test_invalid_config(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises((ValueError, SetupPayloadError), match=message):
        EntryModelConfig.from_parameters(**kwargs)  # type: ignore[arg-type]


def test_config_rejects_unsupported_kind_and_nonfinite() -> None:
    with pytest.raises(SetupPayloadError):
        EntryModelConfig.from_parameters(default_entry_kind="unsupported")
    with pytest.raises(SetupPayloadError):
        EntryModelConfig.from_parameters(retracement_fraction="NaN")
