"""Orientation validation helpers."""

from __future__ import annotations

from decimal import Decimal

from ..contracts import Direction, FibonacciLevel


def validate_orientation(
    levels: tuple[FibonacciLevel, ...],
    direction: Direction,
) -> None:
    by_ratio = {level.ratio: level.price for level in levels}
    required = {Decimal("0"), Decimal("1")}
    if not required.issubset(by_ratio):
        raise ValueError("orientation requires ratios 0 and 1")
    start = by_ratio[Decimal("1")]
    end = by_ratio[Decimal("0")]
    negatives = [price for ratio, price in by_ratio.items() if ratio < 0]
    if direction == "bullish":
        if end <= start or any(price <= end for price in negatives):
            raise ValueError("invalid bullish orientation")
    else:
        if end >= start or any(price >= end for price in negatives):
            raise ValueError("invalid bearish orientation")
