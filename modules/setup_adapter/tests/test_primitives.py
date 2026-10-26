from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from modular_setup_adapter.primitives import (
    aware_datetime,
    decimal_text,
    decimal_value,
    primitive,
    required_text,
    sha256_hex,
)


def test_decimal_parsing_and_canonical_text() -> None:
    assert decimal_value("1.2300", "price") == Decimal("1.2300")
    assert decimal_value(0, "zero", positive=False) == Decimal("0")
    assert decimal_text(Decimal("1.2300")) == "1.23"
    assert decimal_text(Decimal("0.000")) == "0"


@pytest.mark.parametrize("value", [True, "bad", "NaN", "Infinity", 0, -1])
def test_invalid_positive_decimal_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        decimal_value(value, "price")


def test_aware_datetime_accepts_datetime_z_and_offset() -> None:
    expected = datetime(2026, 1, 1, tzinfo=UTC)
    assert aware_datetime(expected, "time") == expected
    assert aware_datetime("2026-01-01T00:00:00Z", "time") == expected
    assert aware_datetime("2026-01-01T01:00:00+01:00", "time") == expected


@pytest.mark.parametrize("value", ["", "bad-date", datetime(2026, 1, 1), 123])
def test_invalid_datetime_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        aware_datetime(value, "time")


def test_required_text_and_primitive_normalization() -> None:
    now = datetime(2026, 1, 1, tzinfo=UTC)
    value = {
        "none": None,
        "bool": True,
        "int": 1,
        "float": 1.5,
        "decimal": Decimal("2.500"),
        "time": now,
        "nested": ["x", Decimal("3.0")],
    }
    normalized = primitive(value)
    assert normalized == {
        "none": None,
        "bool": True,
        "int": 1,
        "float": 1.5,
        "decimal": "2.5",
        "time": now.isoformat(),
        "nested": ["x", "3"],
    }
    assert required_text("  x  ", "name") == "x"
    assert len(sha256_hex(value)) == 64
    assert sha256_hex(value) == sha256_hex(value)


@pytest.mark.parametrize("value", [None, "   "])
def test_required_text_rejects_empty(value: object) -> None:
    with pytest.raises(ValueError):
        required_text(value, "name")


@pytest.mark.parametrize(
    "value",
    [
        float("inf"),
        Decimal("NaN"),
        datetime(2026, 1, 1),
        {1: "bad"},
        object(),
    ],
)
def test_primitive_rejects_non_json_safe_values(value: object) -> None:
    with pytest.raises(ValueError):
        primitive(value)
