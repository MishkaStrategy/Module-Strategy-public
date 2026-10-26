from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_core.validation import (
    all_finite,
    finite_number,
    nonnegative_number,
    positive_number,
    require_nonblank,
    utc_datetime,
)


@pytest.mark.parametrize("value", ["1.25", b"1.25", True, object()])
def test_finite_number_rejects_coercible_or_non_numeric_values(value: object) -> None:
    with pytest.raises(TypeError, match="must be numeric"):
        finite_number(value, "value")  # type: ignore[arg-type]


def test_finite_number_accepts_decimal_without_string_coercion() -> None:
    assert finite_number(Decimal("1.25"), "value") == 1.25
    assert positive_number(Decimal("0.01"), "value") == 0.01
    assert nonnegative_number(Decimal("0"), "value") == 0.0


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), float("inf")])
def test_finite_number_rejects_non_finite_values(value: object) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        finite_number(value, "value")  # type: ignore[arg-type]


def test_all_finite_applies_the_same_strict_boundary() -> None:
    all_finite((1, 2.5, Decimal("3.5")), "values")
    with pytest.raises(TypeError, match="must be numeric"):
        all_finite((1, "2"), "values")  # type: ignore[list-item]


def test_require_nonblank_distinguishes_type_and_blank_errors() -> None:
    with pytest.raises(TypeError, match="must be str"):
        require_nonblank(1, "identifier")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="is required"):
        require_nonblank("   ", "identifier")
    assert require_nonblank("value", "identifier") == "value"


def test_utc_datetime_has_explicit_type_awareness_and_normalization() -> None:
    with pytest.raises(TypeError, match="must be datetime"):
        utc_datetime("2026-01-01", "timestamp")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        utc_datetime(datetime(2026, 1, 1), "timestamp")
    source = datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
    assert utc_datetime(source, "timestamp") == datetime(
        2026,
        1,
        1,
        tzinfo=timezone.utc,
    )
