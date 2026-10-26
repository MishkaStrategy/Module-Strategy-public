from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any

from modular_core import Candle

DECIMAL_CONTEXT = Context(prec=34, rounding=ROUND_HALF_EVEN)


def _require_exact_keys(payload: Mapping[str, Any], allowed: frozenset[str], label: str) -> None:
    keys = set(payload)
    if any(not isinstance(key, str) for key in keys):
        raise ValueError(f"{label} keys must be strings")
    unknown = sorted(keys - allowed)
    if unknown:
        raise ValueError(f"unknown {label} keys: {', '.join(unknown)}")
    missing = sorted(allowed - keys)
    if missing:
        raise ValueError(f"missing {label} keys: {', '.join(missing)}")


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _parse_aware_utc(value: object, field_name: str) -> datetime:
    text = _require_string(value, field_name)
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return parsed.astimezone(UTC)


def _parse_date(value: object, field_name: str) -> date:
    text = _require_string(value, field_name)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO-8601 date") from exc


def _parse_decimal(value: object, field_name: str) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"{field_name} must be a decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{field_name} must be a decimal string") from exc
    if not parsed.is_finite():
        raise ValueError(f"{field_name} must be finite")
    return parsed


@dataclass(slots=True)
class RangeAccumulator:
    session_name: str
    symbol: str
    timeframe: str
    anchor_date: date
    start_utc: datetime
    end_utc: datetime
    first_open: Decimal
    high: Decimal
    low: Decimal
    last_close: Decimal
    volume: Decimal
    bar_count: int
    next_open: datetime

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.session_name, self.symbol, self.timeframe)
        ):
            raise ValueError("range identity fields cannot be empty")
        if not isinstance(self.anchor_date, date) or isinstance(self.anchor_date, datetime):
            raise ValueError("range anchor_date must be a date")
        for field_name, value in (
            ("start_utc", self.start_utc),
            ("end_utc", self.end_utc),
            ("next_open", self.next_open),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"{field_name} must be timezone-aware")
            setattr(self, field_name, value.astimezone(UTC))
        if self.end_utc <= self.start_utc:
            raise ValueError("range end must be after start")
        numeric = (self.first_open, self.high, self.low, self.last_close, self.volume)
        if any(not isinstance(value, Decimal) or not value.is_finite() for value in numeric):
            raise ValueError("range numeric state must be finite Decimal values")
        if any(value <= 0 for value in (self.first_open, self.high, self.low, self.last_close)):
            raise ValueError("range OHLC values must be positive")
        if (
            self.high < max(self.first_open, self.last_close)
            or self.low > min(self.first_open, self.last_close)
            or self.high < self.low
        ):
            raise ValueError("range OHLC geometry is invalid")
        if isinstance(self.bar_count, bool) or not isinstance(self.bar_count, int):
            raise ValueError("range bar_count must be an integer")
        if self.bar_count < 1:
            raise ValueError("range bar_count must be positive")
        if not self.start_utc < self.next_open <= self.end_utc:
            raise ValueError("range next_open must be inside (start, end]")
        if self.volume < 0:
            raise ValueError("range volume cannot be negative")

    @classmethod
    def start(
        cls,
        session_name: str,
        anchor_date: date,
        start_utc: datetime,
        end_utc: datetime,
        bar: Candle,
    ) -> RangeAccumulator:
        return cls(
            session_name=session_name,
            symbol=bar.symbol,
            timeframe=bar.timeframe,
            anchor_date=anchor_date,
            start_utc=start_utc,
            end_utc=end_utc,
            first_open=Decimal(str(bar.open)),
            high=Decimal(str(bar.high)),
            low=Decimal(str(bar.low)),
            last_close=Decimal(str(bar.close)),
            volume=Decimal(str(bar.volume)),
            bar_count=1,
            next_open=bar.close_time,
        )

    def push(self, bar: Candle) -> None:
        self.high = max(self.high, Decimal(str(bar.high)))
        self.low = min(self.low, Decimal(str(bar.low)))
        self.last_close = Decimal(str(bar.close))
        self.volume = DECIMAL_CONTEXT.add(self.volume, Decimal(str(bar.volume)))
        self.bar_count += 1
        self.next_open = bar.close_time.astimezone(UTC)

    @property
    def width(self) -> Decimal:
        return DECIMAL_CONTEXT.subtract(self.high, self.low)

    @property
    def midpoint(self) -> Decimal:
        total = DECIMAL_CONTEXT.add(self.high, self.low)
        return DECIMAL_CONTEXT.divide(total, Decimal("2"))

    def to_mapping(self) -> dict[str, object]:
        return {
            "session_name": self.session_name,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "anchor_date": self.anchor_date.isoformat(),
            "start_utc": self.start_utc.isoformat(),
            "end_utc": self.end_utc.isoformat(),
            "first_open": str(self.first_open),
            "high": str(self.high),
            "low": str(self.low),
            "last_close": str(self.last_close),
            "volume": str(self.volume),
            "bar_count": self.bar_count,
            "next_open": self.next_open.isoformat(),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> RangeAccumulator:
        allowed = frozenset(
            {
                "session_name",
                "symbol",
                "timeframe",
                "anchor_date",
                "start_utc",
                "end_utc",
                "first_open",
                "high",
                "low",
                "last_close",
                "volume",
                "bar_count",
                "next_open",
            }
        )
        _require_exact_keys(payload, allowed, "range accumulator")
        bar_count = payload["bar_count"]
        if isinstance(bar_count, bool) or not isinstance(bar_count, int):
            raise ValueError("range bar_count must be an integer")
        return cls(
            session_name=_require_string(payload["session_name"], "session_name"),
            symbol=_require_string(payload["symbol"], "symbol"),
            timeframe=_require_string(payload["timeframe"], "timeframe"),
            anchor_date=_parse_date(payload["anchor_date"], "anchor_date"),
            start_utc=_parse_aware_utc(payload["start_utc"], "start_utc"),
            end_utc=_parse_aware_utc(payload["end_utc"], "end_utc"),
            first_open=_parse_decimal(payload["first_open"], "first_open"),
            high=_parse_decimal(payload["high"], "high"),
            low=_parse_decimal(payload["low"], "low"),
            last_close=_parse_decimal(payload["last_close"], "last_close"),
            volume=_parse_decimal(payload["volume"], "volume"),
            bar_count=bar_count,
            next_open=_parse_aware_utc(payload["next_open"], "next_open"),
        )


@dataclass(frozen=True, slots=True)
class BarCursor:
    open_time: datetime
    close_time: datetime
    fingerprint: tuple[float, float, float, float, float]

    def __post_init__(self) -> None:
        for field_name, value in (("open_time", self.open_time), ("close_time", self.close_time)):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"bar cursor {field_name} must be timezone-aware")
            object.__setattr__(self, field_name, value.astimezone(UTC))
        if self.close_time <= self.open_time:
            raise ValueError("bar cursor close_time must be after open_time")
        if len(self.fingerprint) != 5 or any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            for value in self.fingerprint
        ):
            raise ValueError("bar cursor fingerprint must contain five finite values")
        open_price, high, low, close, volume = (float(value) for value in self.fingerprint)
        canonical = (open_price, high, low, close, volume)
        object.__setattr__(self, "fingerprint", canonical)
        if any(value <= 0 for value in (open_price, high, low, close)) or volume < 0:
            raise ValueError(
                "bar cursor fingerprint OHLC values must be positive and volume nonnegative"
            )
        if high < max(open_price, close) or low > min(open_price, close) or high < low:
            raise ValueError("bar cursor fingerprint OHLC geometry is invalid")

    @classmethod
    def from_candle(cls, bar: Candle) -> BarCursor:
        # Candle is a validated, immutable Core DTO. Avoid repeating its finite/OHLC
        # validation on the per-bar hot path; strict validation remains in from_mapping.
        cursor = object.__new__(cls)
        object.__setattr__(cursor, "open_time", bar.timestamp)
        object.__setattr__(cursor, "close_time", bar.close_time)
        object.__setattr__(
            cursor, "fingerprint", (bar.open, bar.high, bar.low, bar.close, bar.volume)
        )
        return cursor

    def to_mapping(self) -> dict[str, object]:
        return {
            "open_time": self.open_time.isoformat(),
            "close_time": self.close_time.isoformat(),
            "fingerprint": list(self.fingerprint),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> BarCursor:
        allowed = frozenset({"open_time", "close_time", "fingerprint"})
        _require_exact_keys(payload, allowed, "bar cursor")
        raw = payload["fingerprint"]
        if not isinstance(raw, (list, tuple)) or len(raw) != 5:
            raise ValueError("bar cursor fingerprint must contain five values")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in raw):
            raise ValueError("bar cursor fingerprint must contain numeric values")
        return cls(
            open_time=_parse_aware_utc(payload["open_time"], "open_time"),
            close_time=_parse_aware_utc(payload["close_time"], "close_time"),
            fingerprint=(
                float(raw[0]),
                float(raw[1]),
                float(raw[2]),
                float(raw[3]),
                float(raw[4]),
            ),
        )


@dataclass(frozen=True, slots=True)
class StoredFeature:
    key: str
    feature_id: str
    symbol: str
    timeframe: str
    created_at: datetime
    valid_until: datetime
    value: Mapping[str, object]
    metadata: Mapping[str, object]

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value
            for value in (self.key, self.feature_id, self.symbol, self.timeframe)
        ):
            raise ValueError("stored feature identity fields cannot be empty")
        for field_name, value in (
            ("created_at", self.created_at),
            ("valid_until", self.valid_until),
        ):
            if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
                raise ValueError(f"stored feature {field_name} must be timezone-aware")
            object.__setattr__(self, field_name, value.astimezone(UTC))
        if self.valid_until < self.created_at:
            raise ValueError("stored feature valid_until cannot precede created_at")
        if not isinstance(self.value, Mapping) or not isinstance(self.metadata, Mapping):
            raise ValueError("stored feature value and metadata must be mappings")
        object.__setattr__(self, "value", MappingProxyType(dict(self.value)))
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))

    def to_mapping(self) -> dict[str, object]:
        return {
            "key": self.key,
            "feature_id": self.feature_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "created_at": self.created_at.isoformat(),
            "valid_until": self.valid_until.isoformat(),
            "value": dict(self.value),
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> StoredFeature:
        allowed = frozenset(
            {
                "key",
                "feature_id",
                "symbol",
                "timeframe",
                "created_at",
                "valid_until",
                "value",
                "metadata",
            }
        )
        _require_exact_keys(payload, allowed, "stored feature")
        value = payload["value"]
        metadata = payload["metadata"]
        if not isinstance(value, Mapping) or not isinstance(metadata, Mapping):
            raise ValueError("stored feature value and metadata must be mappings")
        return cls(
            key=_require_string(payload["key"], "key"),
            feature_id=_require_string(payload["feature_id"], "feature_id"),
            symbol=_require_string(payload["symbol"], "symbol"),
            timeframe=_require_string(payload["timeframe"], "timeframe"),
            created_at=_parse_aware_utc(payload["created_at"], "created_at"),
            valid_until=_parse_aware_utc(payload["valid_until"], "valid_until"),
            value=dict(value),
            metadata=dict(metadata),
        )
