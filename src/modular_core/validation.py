from __future__ import annotations

import math
from datetime import datetime, timezone
from decimal import Decimal
from typing import Iterable

Numeric = int | float | Decimal


def utc_datetime(value: datetime, name: str) -> datetime:
    """Return an aware datetime normalized to UTC.

    The kernel accepts any aware input timezone at its boundary but stores and
    compares every timestamp in UTC. Naive datetimes are rejected because
    silently guessing a timezone would make backtests non-reproducible.
    """

    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def finite_number(value: Numeric, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(f"{name} must be numeric")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError(f"{name} must be finite")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{name} must be finite")
    return number


def positive_number(value: Numeric, name: str) -> float:
    number = finite_number(value, name)
    if number <= 0:
        raise ValueError(f"{name} must be positive")
    return number


def nonnegative_number(value: Numeric, name: str) -> float:
    number = finite_number(value, name)
    if number < 0:
        raise ValueError(f"{name} cannot be negative")
    return number


def require_nonblank(value: str, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    if not value.strip():
        raise ValueError(f"{name} is required")
    return value


def all_finite(values: Iterable[Numeric], name: str) -> None:
    for value in values:
        finite_number(value, name)
