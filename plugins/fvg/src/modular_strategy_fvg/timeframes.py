from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

_TIMEFRAME_RE = re.compile(r"^([1-9][0-9]*)([smhdw])$")
_SECONDS_BY_UNIT = {"s": 1, "m": 60, "h": 3600, "d": 86_400, "w": 604_800}
_WEEK_ANCHOR = datetime(1970, 1, 5, tzinfo=timezone.utc)  # Monday 00:00 UTC.
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_MIN_UTC = datetime.min.replace(tzinfo=timezone.utc)
# Keep any parsed duration representable on both sides of the Unix anchor. This
# prevents regex-valid but operationally impossible timeframes from surfacing as
# low-level ``OverflowError`` exceptions later in candle or period arithmetic.
_MAX_TIMEFRAME_SECONDS = (_EPOCH - _MIN_UTC).days * 86_400


def canonical_timeframe(value: str) -> str:
    if type(value) is not str:
        raise TypeError("timeframe must be a canonical string")
    canonical = value.strip().lower()
    if not _TIMEFRAME_RE.fullmatch(canonical):
        raise ValueError(f"unsupported timeframe: {value!r}")
    return canonical


def timeframe_seconds(value: str) -> int:
    canonical = canonical_timeframe(value)
    match = _TIMEFRAME_RE.fullmatch(canonical)
    if match is None:
        raise ValueError(f"unsupported timeframe: {value!r}")
    amount_text, unit = match.groups()
    unit_seconds = _SECONDS_BY_UNIT[unit]
    maximum_amount_text = str(_MAX_TIMEFRAME_SECONDS // unit_seconds)
    if len(amount_text) > len(maximum_amount_text) or (
        len(amount_text) == len(maximum_amount_text) and amount_text > maximum_amount_text
    ):
        raise ValueError(f"timeframe duration exceeds supported datetime range: {canonical!r}")
    return int(amount_text) * unit_seconds


def timeframe_delta(value: str) -> timedelta:
    return timedelta(seconds=timeframe_seconds(value))


def as_utc(value: datetime, name: str = "timestamp") -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be a datetime")
    if value.tzinfo is timezone.utc:
        return value
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        offset = value.utcoffset()
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} cannot be represented in UTC") from exc
    if offset is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        return value.astimezone(timezone.utc)
    except (OverflowError, TypeError, ValueError) as exc:
        raise ValueError(f"{name} cannot be represented in UTC") from exc


def parse_canonical_utc_datetime(value: object, name: str = "timestamp") -> datetime:
    """Parse the single UTC ISO representation emitted by checkpoints.

    Equivalent spellings such as ``Z``, non-zero offsets, a space separator,
    or redundant fractional zeros are rejected so one logical checkpoint has
    one byte-level representation.
    """

    if type(value) is not str:
        raise TypeError(f"{name} must be a canonical ISO datetime string in UTC")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} is not a valid ISO datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    try:
        canonical = parsed.astimezone(timezone.utc)
    except OverflowError as exc:
        raise ValueError(f"{name} cannot be represented in UTC") from exc
    if value != canonical.isoformat():
        raise ValueError(f"{name} must be a canonical ISO datetime string in UTC")
    return canonical


def period_open(value: datetime, timeframe: str) -> datetime:
    instant = as_utc(value)
    canonical = canonical_timeframe(timeframe)
    period = timedelta(seconds=timeframe_seconds(canonical))
    anchor = _WEEK_ANCHOR if canonical.endswith("w") else _EPOCH
    slots = (instant - anchor) // period
    try:
        return anchor + slots * period
    except OverflowError as exc:
        raise ValueError("timeframe period open exceeds supported datetime range") from exc


def period_close(value: datetime, timeframe: str) -> datetime:
    opened = period_open(value, timeframe)
    try:
        return opened + timeframe_delta(timeframe)
    except OverflowError as exc:
        raise ValueError("timeframe period close exceeds supported datetime range") from exc


def is_period_aligned(open_time: datetime, timeframe: str) -> bool:
    instant = as_utc(open_time, "open_time")
    return instant == period_open(instant, timeframe)
