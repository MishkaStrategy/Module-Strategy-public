from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from conftest import FakeCandle, bearish_triplet, bullish_triplet
from modular_strategy_fvg import FvgConfig, FvgDirection, candle_fingerprint, detect_zone
from modular_strategy_fvg.detector import make_zone_id


def test_strict_bullish_formula() -> None:
    zone = detect_zone(*bullish_triplet(), FvgConfig(timeframes=("15m",)))
    assert zone is not None
    assert zone.direction is FvgDirection.BULLISH
    assert zone.lower == Decimal("100.0")
    assert zone.upper == Decimal("105.0")


def test_strict_bearish_formula() -> None:
    zone = detect_zone(*bearish_triplet(), FvgConfig(timeframes=("15m",)))
    assert zone is not None
    assert zone.direction is FvgDirection.BEARISH
    assert zone.lower == Decimal("95.0")
    assert zone.upper == Decimal("100.0")


@pytest.mark.parametrize("direction", ["bullish", "bearish"])
def test_equal_boundary_is_not_gap(direction: str) -> None:
    bars = list(bullish_triplet() if direction == "bullish" else bearish_triplet())
    if direction == "bullish":
        c = bars[2]
        bars[2] = type(c)(c.timestamp, c.open, c.high, bars[0].high, c.close, c.volume, c.symbol, c.timeframe)
    else:
        c = bars[2]
        bars[2] = type(c)(c.timestamp, c.open, bars[0].low, c.low, c.close, c.volume, c.symbol, c.timeframe)
    assert detect_zone(*bars, FvgConfig(timeframes=("15m",))) is None


@pytest.mark.parametrize(("filter_", "expected"), [("both", True), ("bullish", True), ("bearish", False)])
def test_direction_filter_bullish(filter_: str, expected: bool) -> None:
    zone = detect_zone(*bullish_triplet(), FvgConfig.from_parameters(timeframes=("15m",), direction_filter=filter_))
    assert (zone is not None) is expected


@pytest.mark.parametrize(("threshold", "expected"), [("4.999", True), ("5", True), ("5.001", False)])
def test_absolute_threshold_is_inclusive(threshold: str, expected: bool) -> None:
    config = FvgConfig.from_parameters(timeframes=("15m",), min_gap_absolute=threshold)
    assert (detect_zone(*bullish_triplet(), config) is not None) is expected


@pytest.mark.parametrize(
    ("threshold", "expected"), [("0.046", True), ("0.046728", True), ("0.046729", False), ("0.047", False)]
)
def test_fraction_threshold_boundary(threshold: str, expected: bool) -> None:
    config = FvgConfig.from_parameters(timeframes=("15m",), min_gap_fraction=threshold)
    assert (detect_zone(*bullish_triplet(), config) is not None) is expected


def test_nonconsecutive_returns_none_by_default() -> None:
    a, b, c = bullish_triplet()
    shifted = type(c)(
        c.timestamp + timedelta(minutes=15), c.open, c.high, c.low, c.close, c.volume, c.symbol, c.timeframe
    )
    assert detect_zone(a, b, shifted, FvgConfig(timeframes=("15m",))) is None


def test_nonconsecutive_allowed_when_configured() -> None:
    a, b, c = bullish_triplet()
    shifted = type(c)(
        c.timestamp + timedelta(minutes=15), c.open, c.high, c.low, c.close, c.volume, c.symbol, c.timeframe
    )
    config = FvgConfig.from_parameters(timeframes=("15m",), require_consecutive_bars=False)
    assert detect_zone(a, b, shifted, config) is not None


@pytest.mark.parametrize("attribute", ["symbol", "timeframe"])
def test_mixed_contract_rejected(attribute: str) -> None:
    a, b, c = bullish_triplet()
    values = dict(
        timestamp=c.timestamp,
        close_time=c.close_time,
        high=c.high,
        low=c.low,
        close=c.close,
        symbol=c.symbol,
        timeframe=c.timeframe,
    )
    values[attribute] = "OTHER"
    fake = FakeCandle(**values)
    with pytest.raises(ValueError):
        detect_zone(a, b, fake, FvgConfig(timeframes=("15m",)))


@pytest.mark.parametrize(
    ("high", "low", "close"),
    [
        (100.0, 0.0, 50.0),
        (100.0, -1.0, 50.0),
        (100.0, 90.0, 101.0),
        (100.0, 90.0, 89.0),
        (90.0, 100.0, 95.0),
        (float("nan"), 90.0, 95.0),
        (float("inf"), 90.0, 95.0),
    ],
)
def test_invalid_custom_candle_geometry_rejected(high: float, low: float, close: float) -> None:
    a, b, c = bullish_triplet()
    fake = FakeCandle(c.timestamp, c.close_time, high, low, close)
    with pytest.raises(ValueError):
        detect_zone(a, b, fake, FvgConfig(timeframes=("15m",)))


def test_naive_timestamp_rejected() -> None:
    a, b, c = bullish_triplet()
    fake = FakeCandle(c.timestamp.replace(tzinfo=None), c.close_time.replace(tzinfo=None), c.high, c.low, c.close)
    with pytest.raises(ValueError):
        detect_zone(a, b, fake, FvgConfig(timeframes=("15m",)))


def test_equivalent_timezone_instants_have_same_zone_id() -> None:
    instant = datetime(2024, 1, 1, tzinfo=timezone.utc)
    shifted = instant.astimezone(timezone(timedelta(hours=7)))
    kwargs = dict(
        symbol="BTCUSDT", timeframe="15m", direction=FvgDirection.BULLISH, lower=Decimal("1"), upper=Decimal("2")
    )
    assert make_zone_id(source_c_open_time=instant, **kwargs) == make_zone_id(source_c_open_time=shifted, **kwargs)


def test_fingerprint_changes_with_ohlc_but_not_timezone_offset() -> None:
    _, _, c = bullish_triplet()
    equivalent = type(
        "FullCandle",
        (),
        {
            "timestamp": c.timestamp.astimezone(timezone(timedelta(hours=3))),
            "close_time": c.close_time.astimezone(timezone(timedelta(hours=3))),
            "open": c.open,
            "high": c.high,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "symbol": c.symbol,
            "timeframe": c.timeframe,
        },
    )()
    changed = type(
        "FullCandle",
        (),
        {
            "timestamp": c.timestamp,
            "close_time": c.close_time,
            "open": c.open,
            "high": c.high + 1,
            "low": c.low,
            "close": c.close,
            "volume": c.volume,
            "symbol": c.symbol,
            "timeframe": c.timeframe,
        },
    )()
    assert candle_fingerprint(c) == candle_fingerprint(equivalent)
    assert candle_fingerprint(c) != candle_fingerprint(changed)
