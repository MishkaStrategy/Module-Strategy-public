from __future__ import annotations

from decimal import ROUND_HALF_EVEN, Context, Decimal

MAX_DECIMAL_SIGNIFICANT_DIGITS = 256
MAX_DECIMAL_ABS_EXPONENT = 4096

DECIMAL_CONTEXT = Context(
    prec=384,
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    capitals=1,
    clamp=0,
)

MAX_DECIMAL_SERIALIZED_SIGNIFICANT_DIGITS = DECIMAL_CONTEXT.prec
MAX_DECIMAL_TEXT_CHARS = MAX_DECIMAL_ABS_EXPONENT + MAX_DECIMAL_SERIALIZED_SIGNIFICANT_DIGITS + 16

CONFIG_VALIDATION_DECIMAL_CONTEXT = Context(
    prec=(MAX_DECIMAL_SIGNIFICANT_DIGITS + 2 * MAX_DECIMAL_ABS_EXPONENT + 8),
    rounding=ROUND_HALF_EVEN,
    Emin=-999_999,
    Emax=999_999,
    capitals=1,
    clamp=0,
)


def _validate_decimal_limits(
    value: Decimal,
    name: str,
    *,
    max_significant_digits: int,
) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{name} must be Decimal")
    if not value.is_finite():
        raise ValueError(f"{name} must be finite")
    decimal_tuple = value.as_tuple()
    exponent = decimal_tuple.exponent
    if not isinstance(exponent, int):
        raise ValueError(f"{name} must be finite")
    if (
        len(decimal_tuple.digits) > max_significant_digits
        or abs(exponent) > MAX_DECIMAL_ABS_EXPONENT
        or abs(value.adjusted()) > MAX_DECIMAL_ABS_EXPONENT
    ):
        raise ValueError(f"{name} exceeds supported Decimal precision or exponent range")


def validate_decimal_bounds(value: Decimal, name: str) -> None:
    """Validate untrusted configuration or checkpoint Decimal input."""

    _validate_decimal_limits(
        value,
        name,
        max_significant_digits=MAX_DECIMAL_SIGNIFICANT_DIGITS,
    )


def exact_decimal_text(value: Decimal) -> str:
    """Return a bounded, context-independent canonical decimal string.

    Runtime arithmetic is performed with ``DECIMAL_CONTEXT`` precision, which is
    intentionally wider than the accepted untrusted input precision. Formatting
    therefore permits one full runtime-context result while configuration and
    checkpoint parsers continue to enforce ``MAX_DECIMAL_SIGNIFICANT_DIGITS``.
    """

    _validate_decimal_limits(
        value,
        "decimal value",
        max_significant_digits=MAX_DECIMAL_SERIALIZED_SIGNIFICANT_DIGITS,
    )
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def optional_exact_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else exact_decimal_text(value)
