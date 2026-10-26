from __future__ import annotations

from decimal import Decimal


def decimal_text(value: Decimal) -> str:
    """Return a context-independent canonical non-exponent Decimal string."""

    if not value.is_finite():
        raise ValueError("decimal_text requires a finite Decimal")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text
