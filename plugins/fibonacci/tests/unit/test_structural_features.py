from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_strategy_fibonacci.candidates.features import average_true_range, directional_efficiency
from modular_strategy_fibonacci.contracts import Candle
from modular_strategy_fibonacci.pivots.detector import detect_pivots


def _candle(index: int, open_: str, high: str, low: str, close: str) -> Candle:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=15),
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
        volume=Decimal("1"),
    )


def test_atr_and_efficiency_boundaries() -> None:
    one = (_candle(0, "100", "102", "99", "101"),)
    assert average_true_range(one) == 0
    assert directional_efficiency(one) == 0

    flat = (
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "102", "98", "100"),
    )
    assert average_true_range(flat, period=2) == Decimal("3")
    assert directional_efficiency(flat) == 0

    trend = (
        _candle(0, "100", "102", "99", "102"),
        _candle(1, "102", "106", "101", "106"),
        _candle(2, "106", "111", "105", "110"),
    )
    assert Decimal("0") < directional_efficiency(trend) <= Decimal("1")


def test_pivots_validate_windows_and_resolve_equal_extreme_plateau() -> None:
    candles = (
        _candle(0, "100", "101", "99", "100"),
        _candle(1, "100", "102", "98", "101"),
        _candle(2, "101", "105", "97", "104"),
        _candle(3, "104", "105", "100", "102"),
        _candle(4, "102", "103", "99", "100"),
    )
    pivots = detect_pivots(candles, left=1, right=1)
    assert any(item.kind == "high" and item.index == 2 for item in pivots)
    assert any(item.kind == "low" and item.index == 2 for item in pivots)
    assert not any(item.kind == "high" and item.index == 3 for item in pivots)

    with pytest.raises(ValueError):
        detect_pivots(candles, left=0, right=1)
    with pytest.raises(ValueError):
        detect_pivots(candles, left=1, right=0)
