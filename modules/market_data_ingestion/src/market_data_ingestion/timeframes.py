from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

_TIMEFRAME = re.compile(r"^(\d+)([smhdw])$")


def timeframe_seconds(value: str) -> int:
    if not isinstance(value, str):
        raise TypeError("timeframe must be str")
    match = _TIMEFRAME.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError(f"unsupported timeframe: {value!r}")
    amount = int(match.group(1))
    if amount <= 0:
        raise ValueError("timeframe amount must be positive")
    multiplier = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}[match.group(2)]
    return amount * multiplier


def timeframe_delta(value: str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(value))


def is_aligned_epoch(epoch_seconds: int, timeframe: str) -> bool:
    return epoch_seconds % timeframe_seconds(timeframe) == 0


def is_timeframe_aligned(value: datetime, timeframe: str) -> bool:
    if value.tzinfo is None or value.utcoffset() is None:
        return False
    return int(value.astimezone(UTC).timestamp()) % timeframe_seconds(timeframe) == 0


def floor_timeframe(value: datetime, timeframe: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    seconds = timeframe_seconds(timeframe)
    epoch = int(value.astimezone(UTC).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, tz=UTC)
