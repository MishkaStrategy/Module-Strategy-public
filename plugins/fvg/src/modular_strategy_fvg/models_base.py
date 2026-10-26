from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping

from .decimal_utils import deterministic_decimal, parse_canonical_decimal_text
from .timeframes import as_utc, canonical_timeframe, is_period_aligned, parse_canonical_utc_datetime, period_close


class FvgDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


class FvgEvaluationStatus(str, Enum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    UNAVAILABLE_WITH_CURRENT_DATA = "unavailable_with_current_data"


class FvgFilterDecision(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"
    UNAVAILABLE = "unavailable"


class FvgLifecycleStatus(str, Enum):
    DETECTED = "detected"
    ACTIVE = "active"
    PARTIALLY_FILLED = "partially_filled"
    FULLY_FILLED = "fully_filled"
    INVALIDATED = "invalidated"
    EXPIRED = "expired"
    ARCHIVED = "archived"
    PROVISIONAL = "provisional"
    UNAVAILABLE_WITH_CURRENT_DATA = "unavailable_with_current_data"


_FILTER_TRACE_ORDER = ("gap_size", "price", "volume")


def _require_canonical_symbol(value: object, name: str = "symbol") -> str:
    if type(value) is not str:
        raise TypeError(f"{name} must be a canonical string")
    if not value or value.strip() != value or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be a non-empty canonical symbol without whitespace")
    return value


def _require_canonical_mapping_keys(payload: object, label: str) -> Mapping[str, object]:
    if not isinstance(payload, Mapping):
        raise TypeError(f"{label} payload must be a mapping")
    if any(type(key) is not str for key in payload):
        raise ValueError(f"{label} payload keys must be canonical strings")
    return payload


def _finite_float(value: Decimal, name: str) -> float:
    try:
        converted = float(value)
    except (OverflowError, ValueError) as exc:
        raise ValueError(f"{name} cannot be represented as a finite float") from exc
    if not math.isfinite(converted) or (value != 0 and converted == 0.0):
        raise ValueError(f"{name} cannot be represented as a finite float without overflow or underflow")
    return converted


@dataclass(frozen=True, slots=True)
class FvgZone:
    zone_id: str
    symbol: str
    timeframe: str
    direction: FvgDirection
    lower: Decimal
    upper: Decimal
    created_at: datetime
    source_a_open_time: datetime
    source_b_open_time: datetime
    source_c_open_time: datetime

    @deterministic_decimal
    def __post_init__(self) -> None:
        for name in ("zone_id", "symbol", "timeframe"):
            value = getattr(self, name)
            if type(value) is not str:
                raise TypeError(f"{name} must be a canonical string")
        if not isinstance(self.direction, FvgDirection):
            raise TypeError("direction must be an FvgDirection")
        for name in ("lower", "upper"):
            if type(getattr(self, name)) is not Decimal:
                raise TypeError(f"{name} must be a Decimal")
        for name in ("created_at", "source_a_open_time", "source_b_open_time", "source_c_open_time"):
            value = getattr(self, name)
            if not isinstance(value, datetime):
                raise TypeError(f"{name} must be a datetime")
            object.__setattr__(self, name, as_utc(value, name))
        if not self.zone_id or not self.symbol or not self.timeframe:
            raise ValueError("zone_id, symbol and timeframe are required")
        _require_canonical_symbol(self.symbol)
        if canonical_timeframe(self.timeframe) != self.timeframe:
            raise ValueError("timeframe must be canonical")
        if not self.source_a_open_time < self.source_b_open_time < self.source_c_open_time < self.created_at:
            raise ValueError("source candle times must be ordered and precede created_at")
        for name in ("source_a_open_time", "source_b_open_time", "source_c_open_time"):
            if not is_period_aligned(getattr(self, name), self.timeframe):
                raise ValueError(f"{name} must align to timeframe")
        if self.created_at != period_close(self.source_c_open_time, self.timeframe):
            raise ValueError("created_at must equal the source C close time")
        if not self.lower.is_finite() or not self.upper.is_finite():
            raise ValueError("zone boundaries must be finite")
        if self.lower <= 0 or self.upper <= 0:
            raise ValueError("zone boundaries must be positive")
        if self.lower >= self.upper:
            raise ValueError("zone lower boundary must be below upper boundary")
        lower_float = _finite_float(self.lower, "lower")
        upper_float = _finite_float(self.upper, "upper")
        midpoint_float = _finite_float(self.midpoint, "midpoint")
        _finite_float(self.size, "size")
        if not lower_float < midpoint_float < upper_float:
            raise ValueError("zone geometry cannot be represented faithfully as floats")

    @property
    def formation_time(self) -> datetime:
        return self.created_at

    @property
    def signal_available_time(self) -> datetime:
        return self.created_at

    @property
    def source_c_close_time(self) -> datetime:
        return self.created_at

    @property
    @deterministic_decimal
    def midpoint(self) -> Decimal:
        return (self.lower + self.upper) / Decimal("2")

    @property
    @deterministic_decimal
    def size(self) -> Decimal:
        return self.upper - self.lower

    def to_feature_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "zone_id": self.zone_id,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "direction": self.direction.value,
                "lower": _finite_float(self.lower, "lower"),
                "upper": _finite_float(self.upper, "upper"),
                "midpoint": _finite_float(self.midpoint, "midpoint"),
                "size": _finite_float(self.size, "size"),
                "lower_decimal": str(self.lower),
                "upper_decimal": str(self.upper),
                "created_at": self.created_at.isoformat(),
                "source_a_open_time": self.source_a_open_time.isoformat(),
                "source_b_open_time": self.source_b_open_time.isoformat(),
                "source_c_open_time": self.source_c_open_time.isoformat(),
                "source_c_close_time": self.source_c_close_time.isoformat(),
                "formation_time": self.formation_time.isoformat(),
                "signal_available_time": self.signal_available_time.isoformat(),
            }
        )

    def to_state(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "zone_id": self.zone_id,
                "symbol": self.symbol,
                "timeframe": self.timeframe,
                "direction": self.direction.value,
                "lower": str(self.lower),
                "upper": str(self.upper),
                "created_at": self.created_at.isoformat(),
                "source_a_open_time": self.source_a_open_time.isoformat(),
                "source_b_open_time": self.source_b_open_time.isoformat(),
                "source_c_open_time": self.source_c_open_time.isoformat(),
            }
        )

    @classmethod
    def from_state(cls, payload: Mapping[str, object]) -> "FvgZone":
        payload = _require_canonical_mapping_keys(payload, "zone state")
        required = {
            "zone_id",
            "symbol",
            "timeframe",
            "direction",
            "lower",
            "upper",
            "created_at",
            "source_a_open_time",
            "source_b_open_time",
            "source_c_open_time",
        }
        unknown = set(payload) - required
        missing = required - set(payload)
        if unknown or missing:
            raise ValueError(f"invalid zone state keys; missing={sorted(missing)}, unknown={sorted(unknown)}")
        for name in required:
            if type(payload[name]) is not str:
                raise ValueError(f"zone state field {name} must be a canonical string")
        lower = parse_canonical_decimal_text(payload["lower"], "zone lower")
        upper = parse_canonical_decimal_text(payload["upper"], "zone upper")
        return cls(
            zone_id=payload["zone_id"],
            symbol=payload["symbol"],
            timeframe=payload["timeframe"],
            direction=FvgDirection(payload["direction"]),
            lower=lower,
            upper=upper,
            created_at=parse_canonical_utc_datetime(payload["created_at"], "created_at"),
            source_a_open_time=parse_canonical_utc_datetime(payload["source_a_open_time"], "source_a_open_time"),
            source_b_open_time=parse_canonical_utc_datetime(payload["source_b_open_time"], "source_b_open_time"),
            source_c_open_time=parse_canonical_utc_datetime(payload["source_c_open_time"], "source_c_open_time"),
        )
