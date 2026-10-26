from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
import re
import sys
from typing import Any, Mapping


_MAX_PUBLIC_FLOAT = Decimal(str(sys.float_info.max))
_TIMEFRAME_RE = re.compile(r"^(\d+)([smhdw])$")
_TIMEFRAME_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def public_float(value: Decimal) -> float:
    """Convert Decimal diagnostics without leaking NaN or infinity."""
    if not isinstance(value, Decimal):
        raise TypeError("public value must be Decimal")
    if value.is_nan():
        raise ValueError("public Decimal value cannot be NaN")
    if value > _MAX_PUBLIC_FLOAT:
        return sys.float_info.max
    if value < -_MAX_PUBLIC_FLOAT:
        return -sys.float_info.max
    return float(value)


class LiquiditySide(str, Enum):
    HIGH = "high"
    LOW = "low"


class SweepDirection(str, Enum):
    BULLISH = "bullish"
    BEARISH = "bearish"


@dataclass(frozen=True, slots=True)
class BarSnapshot:
    timestamp: datetime
    close_time: datetime
    symbol: str
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.timestamp, datetime):
            raise TypeError("bar timestamp must be a datetime")
        if not isinstance(self.close_time, datetime):
            raise TypeError("bar close_time must be a datetime")
        if self.timestamp.tzinfo is None or self.timestamp.utcoffset() is None:
            raise ValueError("bar timestamp must be timezone-aware")
        if self.close_time.tzinfo is None or self.close_time.utcoffset() is None:
            raise ValueError("bar close_time must be timezone-aware")
        object.__setattr__(self, "timestamp", self.timestamp.astimezone(timezone.utc))
        object.__setattr__(self, "close_time", self.close_time.astimezone(timezone.utc))
        if (
            not isinstance(self.symbol, str)
            or not self.symbol.strip()
            or self.symbol != self.symbol.strip()
        ):
            raise ValueError("bar symbol must be a trimmed non-blank string")
        if (
            not isinstance(self.timeframe, str)
            or not self.timeframe.strip()
            or self.timeframe != self.timeframe.strip()
        ):
            raise ValueError("bar timeframe must be a trimmed non-blank string")
        canonical_timeframe = self.timeframe.lower()
        object.__setattr__(self, "timeframe", canonical_timeframe)
        if self.close_time < self.timestamp:
            raise ValueError("bar close_time cannot be before timestamp")
        self._validate_close_time(canonical_timeframe)
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if not isinstance(value, Decimal):
                raise TypeError(f"bar {name} must be Decimal")
            if value <= 0 or not value.is_finite():
                raise ValueError("bar OHLC values must be positive and finite")
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("bar high is inconsistent with OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("bar low is inconsistent with OHLC")

    def _validate_close_time(self, timeframe: str) -> None:
        match = _TIMEFRAME_RE.fullmatch(timeframe)
        if match is None:
            if self.close_time != self.timestamp:
                raise ValueError("opaque timeframe close_time must equal timestamp")
            return
        amount = int(match.group(1))
        if amount <= 0:
            raise ValueError("bar timeframe interval must be positive")
        if self.close_time == self.timestamp:
            raise ValueError("bar close_time must be after timestamp")
        try:
            expected = self.timestamp + timedelta(
                seconds=amount * _TIMEFRAME_SECONDS[match.group(2)]
            )
        except OverflowError as exc:
            raise ValueError("bar timeframe duration is out of supported range") from exc
        if self.close_time != expected:
            raise ValueError("bar close_time must match timeframe duration")

    def fingerprint(self) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        return self.open, self.high, self.low, self.close

    def to_state(self) -> dict[str, object]:
        return {
            "timestamp": self.timestamp.isoformat(),
            "close_time": self.close_time.isoformat(),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "open": str(self.open),
            "high": str(self.high),
            "low": str(self.low),
            "close": str(self.close),
        }


@dataclass(frozen=True, slots=True)
class LiquidityLevel:
    level_id: str
    feature_id: str
    side: LiquiditySide
    price: Decimal
    symbol: str
    timeframe: str
    pivot_time: datetime
    confirmed_at: datetime
    confirmed_sequence: int

    def __post_init__(self) -> None:
        for name in ("level_id", "feature_id", "symbol", "timeframe"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"level {name} must be a trimmed non-blank string")
        object.__setattr__(self, "timeframe", self.timeframe.lower())
        if not isinstance(self.side, LiquiditySide):
            raise TypeError("level side must be LiquiditySide")
        if not isinstance(self.price, Decimal):
            raise TypeError("level price must be Decimal")
        if self.price <= 0 or not self.price.is_finite():
            raise ValueError("level price must be positive and finite")
        if not isinstance(self.pivot_time, datetime):
            raise TypeError("level pivot_time must be a datetime")
        if not isinstance(self.confirmed_at, datetime):
            raise TypeError("level confirmed_at must be a datetime")
        if self.pivot_time.tzinfo is None or self.pivot_time.utcoffset() is None:
            raise ValueError("level pivot_time must be timezone-aware")
        if self.confirmed_at.tzinfo is None or self.confirmed_at.utcoffset() is None:
            raise ValueError("level confirmed_at must be timezone-aware")
        object.__setattr__(self, "pivot_time", self.pivot_time.astimezone(timezone.utc))
        object.__setattr__(self, "confirmed_at", self.confirmed_at.astimezone(timezone.utc))
        if self.confirmed_at < self.pivot_time:
            raise ValueError("level confirmed_at cannot be before pivot_time")
        if (
            isinstance(self.confirmed_sequence, bool)
            or not isinstance(self.confirmed_sequence, int)
            or self.confirmed_sequence <= 0
        ):
            raise ValueError("level confirmed_sequence must be a positive integer")
        if self.feature_id != f"feat-{self.level_id}":
            raise ValueError("level feature_id must match deterministic level identity")

    def age(self, current_sequence: int) -> int:
        return current_sequence - self.confirmed_sequence

    def to_state(self) -> dict[str, object]:
        return {
            "level_id": self.level_id,
            "feature_id": self.feature_id,
            "side": self.side.value,
            "price": str(self.price),
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "pivot_time": self.pivot_time.isoformat(),
            "confirmed_at": self.confirmed_at.isoformat(),
            "confirmed_sequence": self.confirmed_sequence,
        }


@dataclass(frozen=True, slots=True)
class SweepMeasurement:
    direction: SweepDirection
    penetration_bps: Decimal
    wick_to_body_ratio: Decimal
    wick_fraction: Decimal
    extreme_price: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.direction, SweepDirection):
            raise TypeError("direction must be SweepDirection")
        for name in (
            "penetration_bps",
            "wick_to_body_ratio",
            "wick_fraction",
            "extreme_price",
        ):
            if not isinstance(getattr(self, name), Decimal):
                raise TypeError(f"{name} must be Decimal")
        if not self.penetration_bps.is_finite() or self.penetration_bps < 0:
            raise ValueError("penetration_bps must be finite and non-negative")
        if self.wick_to_body_ratio.is_nan() or self.wick_to_body_ratio < 0:
            raise ValueError("wick_to_body_ratio must be non-negative and not NaN")
        if not self.wick_fraction.is_finite() or not Decimal("0") <= self.wick_fraction <= Decimal(
            "1"
        ):
            raise ValueError("wick_fraction must be finite and in [0, 1]")
        if not self.extreme_price.is_finite() or self.extreme_price <= 0:
            raise ValueError("extreme_price must be positive and finite")

    def as_metadata(self) -> Mapping[str, Any]:
        return {
            "direction": self.direction.value,
            "penetration_bps": public_float(self.penetration_bps),
            "wick_to_body_ratio": public_float(self.wick_to_body_ratio),
            "wick_fraction": public_float(self.wick_fraction),
            "extreme_price": public_float(self.extreme_price),
        }
