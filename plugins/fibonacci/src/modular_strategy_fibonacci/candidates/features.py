"""Transparent structural features for impulse candidates."""

from __future__ import annotations

from decimal import Decimal

from ..contracts import Candle


def average_true_range(candles: tuple[Candle, ...], period: int = 14) -> Decimal:
    if len(candles) < 2:
        return Decimal("0")
    ranges: list[Decimal] = []
    for index, candle in enumerate(candles):
        if index == 0:
            ranges.append(candle.high - candle.low)
            continue
        previous_close = candles[index - 1].close
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
            )
        )
    sample = ranges[-period:]
    return sum(sample, Decimal("0")) / Decimal(len(sample))


def directional_efficiency(candles: tuple[Candle, ...]) -> Decimal:
    if len(candles) < 2:
        return Decimal("0")
    net = abs(candles[-1].close - candles[0].open)
    path = sum((abs(item.close - item.open) for item in candles), Decimal("0"))
    return Decimal("0") if path == 0 else min(Decimal("1"), net / path)
