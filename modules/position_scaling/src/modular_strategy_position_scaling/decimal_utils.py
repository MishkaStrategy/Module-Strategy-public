from __future__ import annotations

from decimal import Decimal, InvalidOperation, localcontext
from typing import Final

ZERO: Final = Decimal("0")
ONE: Final = Decimal("1")


def decimal_from(value: object, name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TypeError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def positive_decimal(value: object, name: str) -> Decimal:
    result = decimal_from(value, name)
    if result <= ZERO:
        raise ValueError(f"{name} must be > 0")
    return result


def nonnegative_decimal(value: object, name: str) -> Decimal:
    result = decimal_from(value, name)
    if result < ZERO:
        raise ValueError(f"{name} must be >= 0")
    return result


def canonical_decimal(value: Decimal) -> str:
    """Return a context-independent, lossless plain-decimal representation."""
    if not value.is_finite():
        raise ValueError("canonical decimal must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text


def weighted_average(
    current_price: Decimal,
    current_quantity: Decimal,
    added_price: Decimal,
    added_quantity: Decimal,
) -> Decimal:
    if current_quantity <= ZERO or added_quantity <= ZERO:
        raise ValueError("weighted average quantities must be positive")
    with localcontext() as context:
        context.prec = max(
            34,
            sum(
                len(value.as_tuple().digits)
                for value in (
                    current_price,
                    current_quantity,
                    added_price,
                    added_quantity,
                )
            )
            + 16,
        )
        return (current_price * current_quantity + added_price * added_quantity) / (
            current_quantity + added_quantity
        )
