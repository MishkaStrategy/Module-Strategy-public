from __future__ import annotations

from collections.abc import Sequence as SequenceABC
from decimal import Decimal, DecimalException
from typing import Sequence

from .decimal_utils import deterministic_decimal
from .detector import (
    CandleLike,
    _optional_candle_attribute,
    _required_candle_attribute,
    _validate_candle,
    as_decimal,
)
from .models import FvgDirection, FvgMetrics, FvgZone
from .timeframes import as_utc, timeframe_seconds

_ZERO = Decimal("0")
_ONE = Decimal("1")
_BPS = Decimal("10000")


def _optional_value(candle: CandleLike, name: str) -> Decimal | None:
    raw = _optional_candle_attribute(candle, "analysis", name)
    return None if raw is None else as_decimal(raw)


def _required_value(candle: CandleLike, name: str) -> Decimal:
    return as_decimal(_required_candle_attribute(candle, "analysis", name))


def candle_range(candle: CandleLike) -> Decimal:
    return _required_value(candle, "high") - _required_value(candle, "low")


def candle_body(candle: CandleLike) -> Decimal | None:
    opened = _optional_value(candle, "open")
    return None if opened is None else abs(_required_value(candle, "close") - opened)


def candle_direction(candle: CandleLike) -> str:
    opened = _optional_value(candle, "open")
    if opened is None:
        return "unknown"
    closed = _required_value(candle, "close")
    if closed > opened:
        return "bullish"
    if closed < opened:
        return "bearish"
    return "neutral"


def _ratio(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    return None if denominator == 0 else numerator / denominator


def simple_average(values: Sequence[Decimal]) -> Decimal | None:
    return None if not values else sum(values, _ZERO) / Decimal(len(values))


def true_range(candle: CandleLike, previous_close: Decimal | None) -> Decimal:
    high = _required_value(candle, "high")
    low = _required_value(candle, "low")
    if previous_close is None:
        return high - low
    return max(high - low, abs(high - previous_close), abs(low - previous_close))


def _validate_range_history(history: Sequence[CandleLike]) -> tuple[CandleLike, ...]:
    if isinstance(history, (str, bytes)) or not isinstance(history, SequenceABC):
        raise TypeError("history must be an ordered sequence of candle values")
    bars = tuple(history)
    expected_symbol: str | None = None
    expected_timeframe: str | None = None
    previous_close = None
    for index, bar in enumerate(bars):
        _validate_candle(bar, f"history[{index}]")
        if expected_symbol is None:
            expected_symbol = bar.symbol
            expected_timeframe = bar.timeframe
        elif bar.symbol != expected_symbol or bar.timeframe != expected_timeframe:
            raise ValueError("range history must use one symbol and timeframe")
        opened = as_utc(bar.timestamp, f"history[{index}].timestamp")
        closed = as_utc(bar.close_time, f"history[{index}].close_time")
        if previous_close is not None and opened < previous_close:
            raise ValueError("range history must be ordered and non-overlapping")
        previous_close = closed
    return bars


def _average_true_range_validated(history: Sequence[CandleLike], period: int) -> Decimal | None:
    if len(history) < period + 1:
        return None
    window = history[-(period + 1) :]
    values: list[Decimal] = []
    previous_close = _required_value(window[0], "close")
    for bar in window[1:]:
        values.append(true_range(bar, previous_close))
        previous_close = _required_value(bar, "close")
    return simple_average(values)


@deterministic_decimal
def average_true_range(history: Sequence[CandleLike], period: int) -> Decimal | None:
    if type(period) is not int:
        raise TypeError("ATR period must be a canonical int")
    if period <= 0:
        raise ValueError("ATR period must be positive")
    return _average_true_range_validated(_validate_range_history(history), period)


def _average_candle_range_validated(history: Sequence[CandleLike], period: int) -> Decimal | None:
    if len(history) < period:
        return None
    return simple_average([candle_range(bar) for bar in history[-period:]])


@deterministic_decimal
def average_candle_range(history: Sequence[CandleLike], period: int) -> Decimal | None:
    if type(period) is not int:
        raise TypeError("average range period must be a canonical int")
    if period <= 0:
        raise ValueError("average range period must be positive")
    return _average_candle_range_validated(_validate_range_history(history), period)


def utc_session(hour: int) -> str:
    if type(hour) is not int:
        raise TypeError("UTC hour must be a canonical int")
    if not 0 <= hour <= 23:
        raise ValueError("UTC hour must be in [0, 23]")
    if hour < 8:
        return "asia"
    if hour < 16:
        return "europe"
    return "us"


def _calculate_metrics_arithmetic(
    *,
    zone: FvgZone,
    a: CandleLike,
    b: CandleLike,
    c: CandleLike,
    history: Sequence[CandleLike],
    reference_price: Decimal | None,
    tick_size: Decimal | None,
    atr_period: int,
    average_range_period: int,
    volume_ratio: Decimal | None = None,
) -> FvgMetrics:
    c_close = _required_value(c, "close")
    size = zone.size
    gap_fraction = size / c_close
    b_range = candle_range(b)
    b_body = candle_body(b)
    opened_b = _optional_value(b, "open")
    b_close = _required_value(b, "close")
    upper_wick: Decimal | None = None
    lower_wick: Decimal | None = None
    body_fraction: Decimal | None = None
    if opened_b is not None and b_range > 0:
        body_fraction = abs(b_close - opened_b) / b_range
        upper_wick = (_required_value(b, "high") - max(opened_b, b_close)) / b_range
        lower_wick = (min(opened_b, b_close) - _required_value(b, "low")) / b_range

    opened_a = _optional_value(a, "open")
    displacement = None if opened_a is None else c_close - opened_a
    total_range = candle_range(a) + b_range + candle_range(c)
    impulse_efficiency = None if opened_a is None else _ratio(abs(c_close - opened_a), total_range)
    if b_range == 0:
        close_strength = None
    elif zone.direction is FvgDirection.BULLISH:
        close_strength = (b_close - _required_value(b, "low")) / b_range
    else:
        close_strength = (_required_value(b, "high") - b_close) / b_range

    atr = _average_true_range_validated(history, atr_period)
    mean_range = _average_candle_range_validated(history, average_range_period)
    distance = None
    if reference_price is not None:
        distance = abs(zone.midpoint - reference_price) / reference_price

    return FvgMetrics(
        gap_size=size,
        gap_fraction=gap_fraction,
        gap_bps=gap_fraction * _BPS,
        gap_ticks=None if tick_size is None else size / tick_size,
        gap_atr=None if atr in {None, _ZERO} else size / atr,
        gap_b_range=None if b_range == 0 else size / b_range,
        gap_average_range=None if mean_range in {None, _ZERO} else size / mean_range,
        candle_b_body=b_body,
        candle_b_body_fraction=body_fraction,
        candle_b_direction=candle_direction(b),
        upper_wick_fraction=upper_wick,
        lower_wick_fraction=lower_wick,
        displacement=displacement,
        impulse_efficiency=impulse_efficiency,
        close_strength=close_strength,
        volume_ratio=volume_ratio,
        distance_from_reference=distance,
        utc_session=utc_session(zone.created_at.hour),
    )


def _calculate_metrics_validated(
    *,
    zone: FvgZone,
    a: CandleLike,
    b: CandleLike,
    c: CandleLike,
    history: Sequence[CandleLike],
    reference_price: Decimal | None,
    tick_size: Decimal | None,
    atr_period: int,
    average_range_period: int,
    volume_ratio: Decimal | None = None,
) -> FvgMetrics:
    try:
        return _calculate_metrics_arithmetic(
            zone=zone,
            a=a,
            b=b,
            c=c,
            history=history,
            reference_price=reference_price,
            tick_size=tick_size,
            atr_period=atr_period,
            average_range_period=average_range_period,
            volume_ratio=volume_ratio,
        )
    except DecimalException as exc:
        raise ValueError("metric arithmetic exceeds supported decimal range") from exc


@deterministic_decimal
def calculate_metrics(
    *,
    zone: FvgZone,
    a: CandleLike,
    b: CandleLike,
    c: CandleLike,
    history: Sequence[CandleLike],
    reference_price: Decimal | None,
    tick_size: Decimal | None,
    atr_period: int,
    average_range_period: int,
    volume_ratio: Decimal | None = None,
) -> FvgMetrics:
    if not isinstance(zone, FvgZone):
        raise TypeError("zone must be an FvgZone")
    if isinstance(history, (str, bytes)) or not isinstance(history, SequenceABC):
        raise TypeError("history must be an ordered sequence of candle values")
    for name, value in (("reference_price", reference_price), ("tick_size", tick_size), ("volume_ratio", volume_ratio)):
        if value is not None and type(value) is not Decimal:
            raise TypeError(f"{name} must be a Decimal or null")
        if value is not None and not value.is_finite():
            raise ValueError(f"{name} must be finite")
    if reference_price is not None and reference_price <= 0:
        raise ValueError("reference_price must be positive")
    if tick_size is not None and tick_size <= 0:
        raise ValueError("tick_size must be finite and positive")
    if volume_ratio is not None and volume_ratio < 0:
        raise ValueError("volume_ratio must be non-negative")

    candles = (("a", a), ("b", b), ("c", c))
    for label, candle in candles:
        _validate_candle(candle, label)
        if candle.symbol != zone.symbol or candle.timeframe != zone.timeframe:
            raise ValueError("metric candle scope differs from zone")
    source_times = (
        (a, zone.source_a_open_time),
        (b, zone.source_b_open_time),
        (c, zone.source_c_open_time),
    )
    if any(as_utc(candle.timestamp) != expected for candle, expected in source_times):
        raise ValueError("metric candles do not match zone source times")

    validated_history = _validate_range_history(history)
    previous_close = None
    for index, bar in enumerate(validated_history):
        _validate_candle(bar, f"history[{index}]")
        if bar.symbol != zone.symbol or bar.timeframe != zone.timeframe:
            raise ValueError("metric history scope differs from zone")
        opened = as_utc(bar.timestamp, f"history[{index}].timestamp")
        closed = as_utc(bar.close_time, f"history[{index}].close_time")
        if previous_close is not None and opened < previous_close:
            raise ValueError("metric history must be ordered and non-overlapping")
        if closed > as_utc(c.close_time, "c.close_time"):
            raise ValueError("metric history cannot contain future bars")
        previous_close = closed

    return _calculate_metrics_validated(
        zone=zone,
        a=a,
        b=b,
        c=c,
        history=validated_history,
        reference_price=reference_price,
        tick_size=tick_size,
        atr_period=atr_period,
        average_range_period=average_range_period,
        volume_ratio=volume_ratio,
    )


@deterministic_decimal
def zone_overlap_fraction(left: FvgZone, right: FvgZone) -> Decimal:
    if not isinstance(left, FvgZone) or not isinstance(right, FvgZone):
        raise TypeError("zone overlap requires FvgZone values")
    if left.symbol != right.symbol:
        raise ValueError("zone overlap requires the same instrument")
    overlap = max(_ZERO, min(left.upper, right.upper) - max(left.lower, right.lower))
    denominator = min(left.size, right.size)
    return _ZERO if denominator == 0 else overlap / denominator


def zones_have_opposite_overlap(left: FvgZone, right: FvgZone) -> bool:
    if not isinstance(left, FvgZone) or not isinstance(right, FvgZone):
        raise TypeError("opposite overlap requires FvgZone values")
    return left.direction is not right.direction and zone_overlap_fraction(left, right) > _ZERO


def higher_timeframe_alignment(zone: FvgZone, context: Sequence[FvgZone]) -> tuple[str, ...]:
    if not isinstance(zone, FvgZone):
        raise TypeError("zone must be an FvgZone")
    if isinstance(context, (str, bytes)) or not isinstance(context, SequenceABC):
        raise TypeError("context must be an ordered sequence of FvgZone values")
    candidates = tuple(context)
    if any(not isinstance(candidate, FvgZone) for candidate in candidates):
        raise TypeError("context must contain only FvgZone values")
    aligned = [
        candidate.zone_id
        for candidate in candidates
        if candidate.symbol == zone.symbol
        and timeframe_seconds(candidate.timeframe) > timeframe_seconds(zone.timeframe)
        and candidate.direction is zone.direction
        and zone_overlap_fraction(zone, candidate) > _ZERO
    ]
    return tuple(sorted(aligned))
