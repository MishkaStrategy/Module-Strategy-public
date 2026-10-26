from __future__ import annotations

import hashlib
from collections.abc import Sequence as SequenceABC
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Protocol, Sequence, cast

from modular_core import Candle as CoreCandle

from .config import FvgConfig, FvgDirectionFilter
from .decimal_utils import canonical_decimal_text, deterministic_decimal
from .models import FvgDirection, FvgZone, _require_canonical_symbol
from .timeframes import as_utc, canonical_timeframe, is_period_aligned, period_close, timeframe_seconds


class CandleLike(Protocol):
    timestamp: datetime
    close_time: datetime
    high: float
    low: float
    close: float
    symbol: str
    timeframe: str


def as_decimal(value: float | int | str | Decimal) -> Decimal:
    if type(value) not in (str, int, float, Decimal):
        raise TypeError("market value must be a canonical decimal-compatible value")
    try:
        parsed = value if type(value) is Decimal else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("market value must be decimal-compatible") from exc
    if not parsed.is_finite():
        raise ValueError("market value must be finite")
    return parsed


def _decimal_identity(value: float | int | str | Decimal) -> str:
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("market value must be finite")
        return f"f:{value.hex()}"
    if type(value) is int:
        return f"i:{value}"
    parsed = as_decimal(value)
    return f"d:{canonical_decimal_text(parsed)}"


def _required_candle_attribute(candle: object, label: str, name: str) -> object:
    try:
        return getattr(candle, name)
    except AttributeError as exc:
        raise TypeError(f"{label} candle must provide {name}") from exc
    except Exception as exc:
        raise ValueError(f"{label} candle attribute {name} cannot be read") from exc


def _optional_candle_attribute(candle: object, label: str, name: str) -> object | None:
    try:
        return getattr(candle, name)
    except AttributeError:
        return None
    except Exception as exc:
        raise ValueError(f"{label} candle attribute {name} cannot be read") from exc


def _validated_candle_attribute(candle: CandleLike, label: str, name: str) -> object:
    if isinstance(candle, CoreCandle):
        return getattr(candle, name)
    return _required_candle_attribute(candle, label, name)


def _validated_optional_candle_attribute(candle: CandleLike, label: str, name: str) -> object | None:
    if isinstance(candle, CoreCandle):
        return getattr(candle, name, None)
    return _optional_candle_attribute(candle, label, name)


def _optional_decimal_identity(candle: CandleLike, name: str) -> str:
    value = _validated_optional_candle_attribute(candle, "fingerprint", name)
    return "<missing>" if value is None else _decimal_identity(value)


def _utc(value: datetime, name: str) -> datetime:
    return as_utc(value, name)


def _validate_candle_clock(candle: CandleLike, label: str) -> tuple[datetime, datetime]:
    opened = _utc(_required_candle_attribute(candle, label, "timestamp"), f"{label}.timestamp")
    closed = _utc(_required_candle_attribute(candle, label, "close_time"), f"{label}.close_time")
    if opened >= closed:
        raise ValueError(f"{label} candle close_time cannot precede or equal timestamp")
    symbol = _required_candle_attribute(candle, label, "symbol")
    timeframe = _required_candle_attribute(candle, label, "timeframe")
    if type(symbol) is not str or type(timeframe) is not str:
        raise TypeError(f"{label} candle symbol and timeframe must be canonical strings")
    _require_canonical_symbol(symbol, f"{label} candle symbol")
    if not timeframe:
        raise ValueError(f"{label} candle timeframe is required")
    if canonical_timeframe(timeframe) != timeframe:
        raise ValueError(f"{label} candle timeframe must be canonical")
    duration = timedelta(seconds=timeframe_seconds(timeframe))
    if closed - opened != duration:
        raise ValueError(f"{label} candle duration does not match timeframe")
    if not is_period_aligned(opened, timeframe):
        raise ValueError(f"{label} candle open is not aligned to its UTC timeframe boundary")
    return opened, closed


def _validate_candle(candle: CandleLike, label: str) -> None:
    _validate_candle_clock(candle, label)
    raw_values = tuple(_required_candle_attribute(candle, label, name) for name in ("high", "low", "close"))
    open_value = _optional_candle_attribute(candle, label, "open")
    volume_value = _optional_candle_attribute(candle, label, "volume")
    all_values = (*raw_values, open_value, volume_value)
    high: float | Decimal
    low: float | Decimal
    close: float | Decimal
    opened_price: float | Decimal | None
    volume: float | Decimal | None
    if all(value is None or type(value) in {int, float} for value in all_values):
        high, low, close = raw_values
        opened_price = open_value
        volume = volume_value
        if any(type(value) is float and not math.isfinite(value) for value in all_values if value is not None):
            raise ValueError("market value must be finite")
    else:
        high = as_decimal(raw_values[0])
        low = as_decimal(raw_values[1])
        close = as_decimal(raw_values[2])
        opened_price = as_decimal(open_value) if open_value is not None else None
        volume = as_decimal(volume_value) if volume_value is not None else None
    if low <= 0 or high <= 0 or close <= 0 or (opened_price is not None and opened_price <= 0):
        raise ValueError(f"{label} candle prices must be positive")
    if low > high or close < low or close > high:
        raise ValueError(f"{label} candle price geometry is invalid")
    if opened_price is not None and not low <= opened_price <= high:
        raise ValueError(f"{label} candle open is outside high/low range")
    if volume is not None and volume < 0:
        raise ValueError(f"{label} candle volume must be non-negative")


def are_consecutive(a: CandleLike, b: CandleLike, c: CandleLike) -> bool:
    return _utc(cast(datetime, _validated_candle_attribute(b, "b", "timestamp")), "b.timestamp") == _utc(
        cast(datetime, _validated_candle_attribute(a, "a", "close_time")), "a.close_time"
    ) and _utc(cast(datetime, _validated_candle_attribute(c, "c", "timestamp")), "c.timestamp") == _utc(
        cast(datetime, _validated_candle_attribute(b, "b", "close_time")), "b.close_time"
    )


def validate_history_bars(
    history: Sequence[CandleLike],
    *,
    symbol: str,
    timeframe: str,
    before: datetime,
    require_consecutive: bool,
) -> None:
    if isinstance(history, (str, bytes)) or not isinstance(history, SequenceABC):
        raise TypeError("history must be an ordered sequence of candle values")
    cutoff = _utc(before, "history.before")
    previous_close: datetime | None = None
    for index, bar in enumerate(history):
        _validate_candle(bar, f"history[{index}]")
        bar_symbol = cast(str, _validated_candle_attribute(bar, f"history[{index}]", "symbol"))
        bar_timeframe = cast(str, _validated_candle_attribute(bar, f"history[{index}]", "timeframe"))
        if bar_symbol != symbol or bar_timeframe != timeframe:
            raise ValueError("history bar scope differs from the FVG window")
        opened = _utc(
            cast(datetime, _validated_candle_attribute(bar, f"history[{index}]", "timestamp")),
            f"history[{index}].timestamp",
        )
        closed = _utc(
            cast(datetime, _validated_candle_attribute(bar, f"history[{index}]", "close_time")),
            f"history[{index}].close_time",
        )
        if closed > cutoff:
            raise ValueError("history bars must close before A")
        if previous_close is not None:
            if opened < previous_close:
                raise ValueError("history bars must be strictly ordered and non-overlapping")
            if require_consecutive and opened != previous_close:
                raise ValueError("history bars must be consecutive")
        previous_close = closed
    if history and require_consecutive and previous_close != cutoff:
        raise ValueError("history tail must connect to A")


def make_zone_id(
    *,
    symbol: str,
    timeframe: str,
    direction: FvgDirection,
    source_c_open_time: datetime,
    lower: Decimal,
    upper: Decimal,
) -> str:
    if type(symbol) is not str or type(timeframe) is not str:
        raise TypeError("symbol and timeframe must be canonical strings")
    _require_canonical_symbol(symbol)
    if not timeframe:
        raise ValueError("timeframe must be non-empty")
    if canonical_timeframe(timeframe) != timeframe:
        raise ValueError("timeframe must be canonical")
    if not isinstance(direction, FvgDirection):
        raise TypeError("direction must be an FvgDirection")
    if type(lower) is not Decimal or type(upper) is not Decimal:
        raise TypeError("zone boundaries must be Decimal values")
    if not lower.is_finite() or not upper.is_finite() or lower <= 0 or lower >= upper:
        raise ValueError("zone boundaries must be finite, positive and ordered")
    source_open = _utc(source_c_open_time, "source_c_open_time")
    if not is_period_aligned(source_open, timeframe):
        raise ValueError("source_c_open_time must be aligned to timeframe")
    period_close(source_open, timeframe)
    canonical_time = str(source_open)
    identity = "|".join((symbol, timeframe, direction.value, canonical_time, str(lower), str(upper)))
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
    return f"fvg:{symbol}:{timeframe}:{direction.value}:{digest}"


def _candle_fingerprint_validated(candle: CandleLike, close_time: datetime | None = None) -> str:
    opened_raw = _validated_candle_attribute(candle, "fingerprint", "timestamp")
    closed_raw = (
        close_time if close_time is not None else _validated_candle_attribute(candle, "fingerprint", "close_time")
    )
    opened = _utc(cast(datetime, opened_raw), "timestamp")
    closed = _utc(cast(datetime, closed_raw), "close_time")
    symbol = cast(str, _validated_candle_attribute(candle, "fingerprint", "symbol"))
    timeframe = cast(str, _validated_candle_attribute(candle, "fingerprint", "timeframe"))
    high = cast(float | int | str | Decimal, _validated_candle_attribute(candle, "fingerprint", "high"))
    low = cast(float | int | str | Decimal, _validated_candle_attribute(candle, "fingerprint", "low"))
    close = cast(float | int | str | Decimal, _validated_candle_attribute(candle, "fingerprint", "close"))
    identity = "|".join(
        (
            symbol,
            timeframe,
            opened.isoformat(),
            closed.isoformat(),
            _optional_decimal_identity(candle, "open"),
            _decimal_identity(high),
            _decimal_identity(low),
            _decimal_identity(close),
            _optional_decimal_identity(candle, "volume"),
        )
    )
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()


def candle_fingerprint(candle: CandleLike, close_time: datetime | None = None) -> str:
    _validate_candle(candle, "fingerprint")
    canonical_close: datetime | None = None
    if close_time is not None:
        canonical_close = _utc(close_time, "fingerprint.close_time")
        actual = _utc(
            cast(datetime, _validated_candle_attribute(candle, "fingerprint", "close_time")),
            "fingerprint.candle.close_time",
        )
        if canonical_close != actual:
            raise ValueError("fingerprint close_time override must match the candle close instant")
    return _candle_fingerprint_validated(candle, canonical_close)


def _detect_geometry_validated(a: CandleLike, b: CandleLike, c: CandleLike, config: FvgConfig) -> FvgZone | None:
    a_symbol = cast(str, _validated_candle_attribute(a, "a", "symbol"))
    b_symbol = cast(str, _validated_candle_attribute(b, "b", "symbol"))
    c_symbol = cast(str, _validated_candle_attribute(c, "c", "symbol"))
    if a_symbol != b_symbol or b_symbol != c_symbol:
        raise ValueError("FVG source candles must have the same symbol")
    a_timeframe = cast(str, _validated_candle_attribute(a, "a", "timeframe"))
    b_timeframe = cast(str, _validated_candle_attribute(b, "b", "timeframe"))
    c_timeframe = cast(str, _validated_candle_attribute(c, "c", "timeframe"))
    if a_timeframe != b_timeframe or b_timeframe != c_timeframe:
        raise ValueError("FVG source candles must have the same timeframe")
    if c_timeframe not in config.timeframes:
        raise ValueError(f"FVG source timeframe is not configured: {c_timeframe}")
    a_open_time = _utc(cast(datetime, _validated_candle_attribute(a, "a", "timestamp")), "a.timestamp")
    b_open_time = _utc(cast(datetime, _validated_candle_attribute(b, "b", "timestamp")), "b.timestamp")
    c_open_time = _utc(cast(datetime, _validated_candle_attribute(c, "c", "timestamp")), "c.timestamp")
    if not (a_open_time < b_open_time < c_open_time):
        raise ValueError("FVG source candles must be temporally ordered")
    if config.require_consecutive_bars and not are_consecutive(a, b, c):
        return None

    a_high = as_decimal(cast(float | int | str | Decimal, _validated_candle_attribute(a, "a", "high")))
    a_low = as_decimal(cast(float | int | str | Decimal, _validated_candle_attribute(a, "a", "low")))
    c_high = as_decimal(cast(float | int | str | Decimal, _validated_candle_attribute(c, "c", "high")))
    c_low = as_decimal(cast(float | int | str | Decimal, _validated_candle_attribute(c, "c", "low")))
    direction: FvgDirection | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    if c_low > a_high and config.direction_filter is not FvgDirectionFilter.BEARISH:
        direction = FvgDirection.BULLISH
        lower, upper = a_high, c_low
    elif c_high < a_low and config.direction_filter is not FvgDirectionFilter.BULLISH:
        direction = FvgDirection.BEARISH
        lower, upper = c_high, a_low
    if direction is None or lower is None or upper is None:
        return None

    c_close_time = _utc(cast(datetime, _validated_candle_attribute(c, "c", "close_time")), "c.close_time")
    zone_id = make_zone_id(
        symbol=c_symbol,
        timeframe=c_timeframe,
        direction=direction,
        source_c_open_time=c_open_time,
        lower=lower,
        upper=upper,
    )
    return FvgZone(
        zone_id=zone_id,
        symbol=c_symbol,
        timeframe=c_timeframe,
        direction=direction,
        lower=lower,
        upper=upper,
        created_at=c_close_time,
        source_a_open_time=a_open_time,
        source_b_open_time=b_open_time,
        source_c_open_time=c_open_time,
    )


def _detect_zone_validated(a: CandleLike, b: CandleLike, c: CandleLike, config: FvgConfig) -> FvgZone | None:
    zone = _detect_geometry_validated(a, b, c, config)
    if zone is None:
        return None
    c_close = as_decimal(cast(float | int | str | Decimal, _validated_candle_attribute(c, "c", "close")))
    if zone.size < config.min_gap_absolute:
        return None
    if zone.size / c_close < config.min_gap_fraction:
        return None
    return zone


@deterministic_decimal
def detect_zone(a: CandleLike, b: CandleLike, c: CandleLike, config: FvgConfig) -> FvgZone | None:
    if not isinstance(config, FvgConfig):
        raise TypeError("config must be an FvgConfig")
    source_rows = tuple(
        (
            _required_candle_attribute(candle, label, "symbol"),
            _required_candle_attribute(candle, label, "timeframe"),
            _required_candle_attribute(candle, label, "timestamp"),
        )
        for label, candle in (("a", a), ("b", b), ("c", c))
    )
    if source_rows[0][0] != source_rows[1][0] or source_rows[1][0] != source_rows[2][0]:
        raise ValueError("FVG source candles must have the same symbol")
    if source_rows[0][1] != source_rows[1][1] or source_rows[1][1] != source_rows[2][1]:
        raise ValueError("FVG source candles must have the same timeframe")
    if not (
        _utc(source_rows[0][2], "a.timestamp")
        < _utc(source_rows[1][2], "b.timestamp")
        < _utc(source_rows[2][2], "c.timestamp")
    ):
        raise ValueError("FVG source candles must be temporally ordered")
    _validate_candle(a, "a")
    _validate_candle(b, "b")
    _validate_candle(c, "c")
    return _detect_zone_validated(a, b, c, config)
