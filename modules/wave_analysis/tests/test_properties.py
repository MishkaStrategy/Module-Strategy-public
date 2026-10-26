from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given, settings
from hypothesis import strategies as st
from modular_core import Candle

from tests.support import market_view
from wave_analysis import WaveAnalysisModule

START = datetime(2024, 1, 1, tzinfo=UTC)


def make_series(prices: list[float]) -> list[Candle]:
    result: list[Candle] = []
    for index, price in enumerate(prices):
        result.append(
            Candle(
                START + timedelta(minutes=index),
                price,
                price + 0.5,
                price - 0.5,
                price,
                100 + index,
                "BTCUSDT",
                "1m",
            )
        )
    return result


def feed(module: WaveAnalysisModule, candles: list[Candle]) -> None:
    history: list[Candle] = []
    for index, candle in enumerate(candles):
        history.append(candle)
        module.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))


price_lists = st.lists(
    st.floats(min_value=10.0, max_value=1000.0, allow_nan=False, allow_infinity=False),
    min_size=8,
    max_size=40,
)


@given(price_lists)
@settings(max_examples=40, deadline=None)
def test_property_deterministic_replay(prices: list[float]) -> None:
    candles = make_series(prices)
    kwargs = dict(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.03",
        maximum_retained_pivots=32,
        maximum_retained_waves=31,
    )
    first = WaveAnalysisModule(**kwargs)
    second = WaveAnalysisModule(**kwargs)
    feed(first, candles)
    feed(second, candles)
    assert first.state_digest == second.state_digest
    assert first.snapshot_state() == second.snapshot_state()


@given(price_lists)
@settings(max_examples=40, deadline=None)
def test_property_prefix_equals_visible_full_history(prices: list[float]) -> None:
    candles = make_series(prices)
    cutoff = max(4, len(candles) // 2)
    kwargs = dict(timeframes=("1m",), primary_timeframe="1m", left_bars=2, right_bars=2)
    prefix = WaveAnalysisModule(**kwargs)
    full = WaveAnalysisModule(**kwargs)
    feed(prefix, candles[:cutoff])
    feed(full, candles)
    prefix_pivots = prefix.snapshot_state()["timeframes"]["1m"]["confirmed_pivots"]
    cutoff_time = candles[cutoff - 1].close_time.isoformat()
    visible = [
        pivot
        for pivot in full.snapshot_state()["timeframes"]["1m"]["confirmed_pivots"]
        if pivot["confirmed_at"] <= cutoff_time
    ]
    assert visible == prefix_pivots


@given(price_lists)
@settings(max_examples=30, deadline=None)
def test_property_zigzag_confirmed_pivots_alternate(prices: list[float]) -> None:
    module = WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.02",
        maximum_retained_pivots=32,
        maximum_retained_waves=31,
    )
    feed(module, make_series(prices))
    pivots = module.snapshot_state()["timeframes"]["1m"]["confirmed_pivots"]
    assert all(pivots[index]["kind"] != pivots[index - 1]["kind"] for index in range(1, len(pivots)))
    assert all(pivot["confirmed_at"] >= pivot["pivot_time"] for pivot in pivots)


def test_numeric_representation_does_not_change_ids_or_state_digest() -> None:
    integer_candles = [
        Candle(START + timedelta(minutes=i), p, p + 1, p - 1, p, 100, "BTCUSDT", "1m")
        for i, p in enumerate((100, 90, 110, 95, 120, 100, 130))
    ]
    float_candles = [
        Candle(START + timedelta(minutes=i), float(p), float(p + 1), float(p - 1), float(p), 100.0, "BTCUSDT", "1m")
        for i, p in enumerate((100, 90, 110, 95, 120, 100, 130))
    ]
    kwargs = dict(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
    )
    integers = WaveAnalysisModule(**kwargs)
    floats = WaveAnalysisModule(**kwargs)
    feed(integers, integer_candles)
    feed(floats, float_candles)
    assert integers.snapshot_state() == floats.snapshot_state()
    assert integers.state_digest == floats.state_digest
