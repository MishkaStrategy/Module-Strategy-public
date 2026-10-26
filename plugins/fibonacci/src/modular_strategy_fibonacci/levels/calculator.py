"""Exact Decimal Fibonacci level calculations."""

from __future__ import annotations

from decimal import Decimal, localcontext
from typing import Iterable

from ..contracts import Direction, FibonacciLevel

DEFAULT_LEVELS: tuple[Decimal, ...] = (
    Decimal("0"),
    Decimal("0.5"),
    Decimal("0.618"),
    Decimal("0.786"),
    Decimal("0.886"),
    Decimal("1"),
    Decimal("-0.18"),
    Decimal("-0.27"),
    Decimal("-0.5"),
    Decimal("-0.618"),
)


def calculate_levels(
    impulse_start: Decimal,
    impulse_end: Decimal,
    direction: Direction,
    levels: Iterable[Decimal] = DEFAULT_LEVELS,
    *,
    precision: int = 34,
) -> tuple[FibonacciLevel, ...]:
    """Calculate prices using end + ratio * (start - end)."""
    if not impulse_start.is_finite() or not impulse_end.is_finite():
        raise ValueError("anchor prices must be finite")
    if impulse_start == impulse_end:
        raise ValueError("impulse anchors must have different prices")
    if direction == "bullish" and impulse_end <= impulse_start:
        raise ValueError("bullish impulse must end above start")
    if direction == "bearish" and impulse_end >= impulse_start:
        raise ValueError("bearish impulse must end below start")
    ratios = tuple(levels)
    if not ratios:
        raise ValueError("levels cannot be empty")
    if len(set(ratios)) != len(ratios):
        raise ValueError("levels must be unique")
    with localcontext() as ctx:
        ctx.prec = precision
        distance = impulse_start - impulse_end
        result = tuple(FibonacciLevel(ratio=ratio, price=impulse_end + ratio * distance) for ratio in ratios)
    return result
