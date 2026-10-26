from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import timedelta
from functools import lru_cache

from .config import DataConfig
from .models import Candle


class DataValidationError(ValueError):
    pass


_TIMEFRAME_RE = re.compile(r"^(\d+)([smhdw])$")


def timeframe_to_seconds(value: str) -> int | None:
    if not isinstance(value, str):
        raise TypeError("timeframe must be str")
    return _timeframe_to_seconds_cached(value)


@lru_cache(maxsize=128)
def _timeframe_to_seconds_cached(value: str) -> int | None:
    match = _TIMEFRAME_RE.match(value.strip().lower())
    if not match:
        return None
    amount = int(match.group(1))
    if amount <= 0:
        return None
    unit = match.group(2)
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[unit]
    return amount * multiplier


def validate_candles(candles: Iterable[Candle], config: DataConfig | None = None) -> list[Candle]:
    if config is None:
        config = DataConfig()
    elif not isinstance(config, DataConfig):
        raise TypeError("config must be DataConfig or None")
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Iterable):
        raise TypeError("candles must be an iterable of Candle")
    result = list(candles)
    if not result:
        raise DataValidationError("no candles supplied")
    if any(not isinstance(candle, Candle) for candle in result):
        raise TypeError("candles must contain Candle")

    previous = None
    symbol = result[0].symbol
    timeframe = result[0].timeframe
    expected_seconds = config.expected_interval_seconds or timeframe_to_seconds(timeframe)
    expected_delta = timedelta(seconds=expected_seconds) if expected_seconds else None

    for candle in result:
        if candle.symbol != symbol:
            raise DataValidationError("mixed symbols are not supported")
        if candle.timeframe != timeframe:
            raise DataValidationError("mixed base timeframes are not supported")
        if previous is not None:
            if candle.timestamp <= previous.timestamp:
                if candle.timestamp == previous.timestamp:
                    raise DataValidationError(
                        f"duplicate candle timestamp: {candle.timestamp.isoformat()}"
                    )
                raise DataValidationError("candles must be strictly chronological")
            if previous.close_time > candle.timestamp:
                raise DataValidationError("candle intervals cannot overlap")
            if (
                config.gap_policy == "reject"
                and expected_delta is not None
                and candle.timestamp - previous.timestamp != expected_delta
            ):
                raise DataValidationError(
                    f"data gap: expected {expected_delta}, got {candle.timestamp - previous.timestamp} "
                    f"before {candle.timestamp.isoformat()}"
                )
        previous = candle
    return result
