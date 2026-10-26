"""Confirmed pivot detection that never uses data after the cutoff."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from ..contracts import Candle

PivotKind = Literal["high", "low"]


@dataclass(frozen=True, slots=True)
class Pivot:
    index: int
    kind: PivotKind
    price: Decimal
    prominence: Decimal


def detect_pivots(
    candles: tuple[Candle, ...],
    *,
    left: int,
    right: int,
) -> tuple[Pivot, ...]:
    if left < 1 or right < 1:
        raise ValueError("left and right must be positive")
    pivots: list[Pivot] = []
    for index in range(left, len(candles) - right):
        candle = candles[index]
        neighborhood = candles[index - left : index + right + 1]
        highs = [item.high for item in neighborhood]
        lows = [item.low for item in neighborhood]
        highest = max(highs)
        lowest = min(lows)
        # Equal-extreme plateaus are resolved deterministically to their first bar.
        if candle.high == highest and highs.index(highest) == left:
            distinct_highs = [value for value in highs if value < highest]
            prominence = highest - max(distinct_highs) if distinct_highs else Decimal("0")
            pivots.append(Pivot(index, "high", candle.high, prominence))
        if candle.low == lowest and lows.index(lowest) == left:
            distinct_lows = [value for value in lows if value > lowest]
            prominence = min(distinct_lows) - lowest if distinct_lows else Decimal("0")
            pivots.append(Pivot(index, "low", candle.low, prominence))
    return tuple(sorted(pivots, key=lambda item: (item.index, item.kind)))
