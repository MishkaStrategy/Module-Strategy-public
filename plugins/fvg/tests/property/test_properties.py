from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hypothesis import given, settings, strategies as st
from modular_core import Candle

from conftest import view_for
from modular_strategy_fvg import FvgConfig, FvgDirection, FvgModule, detect_zone


@st.composite
def bullish_values(draw: st.DrawFn) -> tuple[float, float, float, float]:
    a_high = draw(st.floats(min_value=1, max_value=1e6, allow_nan=False, allow_infinity=False))
    a_low = draw(st.floats(min_value=max(0.01, a_high * 0.5), max_value=a_high, allow_nan=False, allow_infinity=False))
    gap = draw(st.floats(min_value=0.0001, max_value=max(0.001, a_high * 0.1), allow_nan=False, allow_infinity=False))
    c_low = a_high + gap
    c_high = c_low + max(0.0001, gap)
    return a_low, a_high, c_low, c_high


@given(bullish_values())
@settings(max_examples=80, deadline=None)
def test_property_bullish_zone_geometry(values: tuple[float, float, float, float]) -> None:
    a_low, a_high, c_low, c_high = values
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    a = Candle(start, a_high, a_high, a_low, a_high, 1, "BTCUSDT", "15m")
    b = Candle(start + timedelta(minutes=15), a_high, a_high, a_low, a_high, 1, "BTCUSDT", "15m")
    c = Candle(start + timedelta(minutes=30), c_low, c_high, c_low, c_low, 1, "BTCUSDT", "15m")
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    assert zone.direction is FvgDirection.BULLISH
    assert zone.lower < zone.midpoint < zone.upper
    assert zone.size > 0


@given(st.integers(min_value=3, max_value=40), st.integers(min_value=1, max_value=10))
@settings(max_examples=40, deadline=None)
def test_property_active_state_never_exceeds_capacity(bar_count: int, capacity: int) -> None:
    bars: list[Candle] = []
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    for index in range(bar_count):
        value = 100.0 + index * 3
        bars.append(
            Candle(start + timedelta(minutes=15 * index), value + 1, value + 2, value, value + 1, 1, "BTCUSDT", "15m")
        )
    module = FvgModule(timeframes=("15m",), max_active_zones_per_timeframe=capacity)
    module.on_bar(view_for(bars))
    assert len(module.active_zones) <= capacity


@given(st.integers(min_value=3, max_value=30))
@settings(max_examples=30, deadline=None)
def test_property_replay_digest_is_deterministic(bar_count: int) -> None:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    bars = [
        Candle(
            start + timedelta(minutes=15 * i), 101 + i * 3, 102 + i * 3, 100 + i * 3, 101 + i * 3, 1, "BTCUSDT", "15m"
        )
        for i in range(bar_count)
    ]
    first = FvgModule(timeframes=("15m",), max_active_zones_per_timeframe=20)
    second = FvgModule(timeframes=("15m",), max_active_zones_per_timeframe=20)
    first.on_bar(view_for(bars))
    second.on_bar(view_for(bars))
    assert first.snapshot_state() == second.snapshot_state()
