from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from modular_core import Side


class BreakoutDirection(str, Enum):
    ABOVE = "above"
    BELOW = "below"


class BreakoutReturnPhase(str, Enum):
    SCANNING = "scanning"
    WAITING_RETURN = "waiting_return"


class BreakoutReturnReason(str, Enum):
    INSUFFICIENT_REFERENCE = "breakout_return.insufficient_reference"
    DUPLICATE_EVENT_IGNORED = "breakout_return.duplicate_event_ignored"
    DATA_GAP_RESET = "breakout_return.data_gap_reset"
    BREAKOUT_ABOVE = "breakout_return.breakout_above"
    BREAKOUT_BELOW = "breakout_return.breakout_below"
    RETURN_CONFIRMED = "breakout_return.return_confirmed"
    RETURN_TOO_DEEP = "breakout_return.return_too_deep"
    RETURN_EXPIRED = "breakout_return.return_expired"
    REFERENCE_CHANGED = "breakout_return.reference_changed"
    REFERENCE_MISSING = "breakout_return.reference_missing"
    REFERENCE_STALE = "breakout_return.reference_stale"
    REFERENCE_SCHEMA_MISMATCH = "breakout_return.reference_schema_mismatch"


class BreakoutReturnError(RuntimeError):
    """Base module exception."""


class BreakoutReturnDataError(BreakoutReturnError, ValueError):
    """Raised when the module receives a contradictory market stream."""


class BreakoutReturnStateError(BreakoutReturnError, ValueError):
    """Raised when persisted state is corrupt or incompatible."""


@dataclass(frozen=True, slots=True)
class ReferenceRange:
    high: Decimal
    low: Decimal
    identifier: str
    source: str
    created_at: datetime

    def __post_init__(self) -> None:
        if (
            not isinstance(self.identifier, str)
            or not self.identifier.strip()
            or self.identifier != self.identifier.strip()
        ):
            raise ValueError("reference range identifier must be a nonblank trimmed string")
        if (
            not isinstance(self.source, str)
            or not self.source.strip()
            or self.source != self.source.strip()
        ):
            raise ValueError("reference range source must be a nonblank trimmed string")
        if not isinstance(self.high, Decimal) or not isinstance(self.low, Decimal):
            raise ValueError("reference range values must be Decimal")
        if not self.high.is_finite() or not self.low.is_finite():
            raise ValueError("reference range values must be finite")
        if self.high <= 0 or self.low <= 0 or self.high <= self.low:
            raise ValueError("reference range must satisfy high > low > 0")
        if not isinstance(self.created_at, datetime):
            raise ValueError("reference range created_at must be datetime")
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("reference range created_at must be timezone-aware")
        object.__setattr__(self, "created_at", self.created_at.astimezone(timezone.utc))

    @property
    def width(self) -> Decimal:
        return self.high - self.low


@dataclass(slots=True)
class ActiveBreakout:
    setup_id: str
    direction: BreakoutDirection
    signal_side: Side
    reference: ReferenceRange
    breakout_time: datetime
    breakout_sequence: int
    extreme: Decimal
    symbol: str
    timeframe: str
    feature_revision: int = 0

    def __post_init__(self) -> None:
        for value, name in (
            (self.setup_id, "setup_id"),
            (self.symbol, "symbol"),
            (self.timeframe, "timeframe"),
        ):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"active {name} must be a nonblank trimmed string")
        if not isinstance(self.direction, BreakoutDirection):
            raise ValueError("active direction must be BreakoutDirection")
        if not isinstance(self.signal_side, Side):
            raise ValueError("active signal_side must be Side")
        if not isinstance(self.reference, ReferenceRange):
            raise ValueError("active reference must be ReferenceRange")
        if not isinstance(self.breakout_sequence, int) or isinstance(self.breakout_sequence, bool):
            raise ValueError("active breakout_sequence must be an integer")
        if not isinstance(self.feature_revision, int) or isinstance(self.feature_revision, bool):
            raise ValueError("active feature_revision must be an integer")
        if not isinstance(self.extreme, Decimal):
            raise ValueError("active extreme must be Decimal")
        if not isinstance(self.breakout_time, datetime):
            raise ValueError("active breakout_time must be datetime")
        if self.breakout_time.tzinfo is None or self.breakout_time.utcoffset() is None:
            raise ValueError("active breakout_time must be timezone-aware")
        self.breakout_time = self.breakout_time.astimezone(timezone.utc)
        if self.breakout_sequence <= 0:
            raise ValueError("active breakout_sequence must be positive")
        if self.feature_revision < 0:
            raise ValueError("active feature_revision cannot be negative")
        if not self.extreme.is_finite() or self.extreme <= 0:
            raise ValueError("active extreme must be finite and positive")
        expected_side = Side.SELL if self.direction is BreakoutDirection.ABOVE else Side.BUY
        if self.signal_side is not expected_side:
            raise ValueError("active signal_side contradicts breakout direction")
        if self.direction is BreakoutDirection.ABOVE and self.extreme <= self.reference.high:
            raise ValueError("above-breakout extreme must exceed range high")
        if self.direction is BreakoutDirection.BELOW and self.extreme >= self.reference.low:
            raise ValueError("below-breakout extreme must be below range low")
        if self.reference.created_at > self.breakout_time:
            raise ValueError("reference range cannot be created after breakout")


@dataclass(slots=True)
class BreakoutReturnCounters:
    events_seen: int = 0
    bars_processed: int = 0
    duplicate_events: int = 0
    data_gaps: int = 0
    armed_above: int = 0
    armed_below: int = 0
    confirmed: int = 0
    rejected: int = 0
    expired: int = 0
    invalidated: int = 0
    active_feature_republishes: int = 0
    restores: int = 0
    legacy_state_migrations: int = 0

    _FIELDS = (
        "events_seen",
        "bars_processed",
        "duplicate_events",
        "data_gaps",
        "armed_above",
        "armed_below",
        "confirmed",
        "rejected",
        "expired",
        "invalidated",
        "active_feature_republishes",
        "restores",
        "legacy_state_migrations",
    )

    def as_mapping(self) -> Mapping[str, int]:
        return MappingProxyType({name: int(getattr(self, name)) for name in self._FIELDS})

    def as_checkpoint_mapping(self) -> Mapping[str, int]:
        """Return causal counters without replay/restore observation side effects."""

        values = {name: int(getattr(self, name)) for name in self._FIELDS}
        values["events_seen"] = values["bars_processed"]
        values["duplicate_events"] = 0
        values["restores"] = 0
        values["legacy_state_migrations"] = 0
        return MappingProxyType(values)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, object]) -> BreakoutReturnCounters:
        unknown = sorted(set(raw) - set(cls._FIELDS))
        missing = sorted(set(cls._FIELDS) - set(raw))
        if unknown or missing:
            raise ValueError(f"invalid counters keys; missing={missing}, unknown={unknown}")
        values: dict[str, int] = {}
        for name in cls._FIELDS:
            value = raw[name]
            if isinstance(value, bool) or not isinstance(value, (int, str)):
                raise ValueError(f"counter {name} must be an integer")
            try:
                parsed = int(value)
            except ValueError as exc:
                raise ValueError(f"counter {name} must be an integer") from exc
            if parsed < 0:
                raise ValueError(f"counter {name} cannot be negative")
            values[name] = parsed
        result = cls(**values)
        if result.bars_processed + result.duplicate_events > result.events_seen:
            raise ValueError("counter totals exceed events_seen")
        return result
