from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, DecimalException
from types import MappingProxyType
from typing import Any, Mapping

from .decimal_utils import deterministic_decimal, parse_canonical_decimal_text
from .detector import CandleLike, _validate_candle, as_decimal
from .models import FvgDirection, FvgLifecycleStatus, FvgZone, _require_canonical_mapping_keys
from .timeframes import as_utc, parse_canonical_utc_datetime, timeframe_seconds

_ZERO = Decimal("0")
_ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class FvgZoneLifecycle:
    zone_id: str
    status: FvgLifecycleStatus
    detected_at: datetime
    last_transition_at: datetime
    last_observed_at: datetime
    first_touch_at: datetime | None = None
    last_touch_at: datetime | None = None
    contact_count: int = 0
    fill_percentage: Decimal = _ZERO
    maximum_penetration: Decimal = _ZERO
    age_bars: int = 0

    @deterministic_decimal
    def __post_init__(self) -> None:
        if type(self.zone_id) is not str:
            raise TypeError("zone_id must be a canonical string")
        if not self.zone_id:
            raise ValueError("zone_id is required")
        if not isinstance(self.status, FvgLifecycleStatus):
            raise TypeError("status must be an FvgLifecycleStatus")
        for name in (
            "detected_at",
            "last_transition_at",
            "last_observed_at",
            "first_touch_at",
            "last_touch_at",
        ):
            value = getattr(self, name)
            if value is not None:
                if not isinstance(value, datetime):
                    raise TypeError(f"{name} must be a datetime or null")
                object.__setattr__(self, name, as_utc(value, name))
        if self.last_transition_at < self.detected_at:
            raise ValueError("last_transition_at cannot precede detection")
        if self.last_observed_at < self.detected_at or self.last_transition_at > self.last_observed_at:
            raise ValueError("last_observed_at must cover the latest transition")
        if type(self.contact_count) is not int:
            raise TypeError("contact_count must be a canonical int")
        if self.contact_count < 0:
            raise ValueError("contact_count must be a non-negative int")
        if type(self.age_bars) is not int:
            raise TypeError("age_bars must be a canonical int")
        if self.age_bars < 0:
            raise ValueError("age_bars must be a non-negative int")
        for name in ("fill_percentage", "maximum_penetration"):
            value = getattr(self, name)
            if type(value) is not Decimal:
                raise TypeError(f"{name} must be a Decimal")
            if not value.is_finite() or not _ZERO <= value <= _ONE:
                raise ValueError(f"{name} must be in [0, 1]")
        if self.fill_percentage > self.maximum_penetration:
            raise ValueError("fill percentage cannot exceed maximum penetration")
        if self.contact_count == 0:
            if self.first_touch_at is not None or self.last_touch_at is not None:
                raise ValueError("touch timestamps require at least one contact")
        else:
            if self.first_touch_at is None or self.last_touch_at is None:
                raise ValueError("contacts require both touch timestamps")
            if not self.detected_at <= self.first_touch_at <= self.last_touch_at <= self.last_observed_at:
                raise ValueError("touch timestamps must be ordered within the lifecycle interval")
            if self.contact_count == 1 and self.first_touch_at != self.last_touch_at:
                raise ValueError("single contact requires identical first and last touch timestamps")
            if self.contact_count > 1 and self.first_touch_at >= self.last_touch_at:
                raise ValueError("multiple contacts require strictly increasing touch timestamps")
        if self.contact_count > self.age_bars:
            raise ValueError("contact_count cannot exceed age_bars")
        if self.age_bars == 0 and self.last_observed_at != self.detected_at:
            raise ValueError("zero-age lifecycle must remain at the detection instant")
        if (
            self.status
            in {
                FvgLifecycleStatus.INVALIDATED,
                FvgLifecycleStatus.EXPIRED,
                FvgLifecycleStatus.ARCHIVED,
            }
            and self.last_transition_at != self.last_observed_at
        ):
            raise ValueError("terminal lifecycle transition must be the latest observation")
        if self.status in {FvgLifecycleStatus.DETECTED, FvgLifecycleStatus.ACTIVE}:
            if self.maximum_penetration != _ZERO or self.fill_percentage != _ZERO:
                raise ValueError("active lifecycle cannot contain positive penetration")
        elif self.status is FvgLifecycleStatus.PARTIALLY_FILLED:
            if not _ZERO < self.maximum_penetration < _ONE or self.contact_count == 0:
                raise ValueError("partial lifecycle requires bounded penetration and contact")
            if not self.first_touch_at <= self.last_transition_at <= self.last_touch_at:
                raise ValueError("partial lifecycle transition must occur within the touch interval")
        elif self.status is FvgLifecycleStatus.FULLY_FILLED:
            if self.maximum_penetration != _ONE or self.contact_count == 0:
                raise ValueError("fully filled lifecycle requires complete maximum penetration")
            if not self.first_touch_at <= self.last_transition_at <= self.last_touch_at:
                raise ValueError("fully filled lifecycle transition must occur within the touch interval")

    def to_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "zone_id": self.zone_id,
                "status": self.status.value,
                "detected_at": self.detected_at.isoformat(),
                "last_transition_at": self.last_transition_at.isoformat(),
                "last_observed_at": self.last_observed_at.isoformat(),
                "first_touch_at": None if self.first_touch_at is None else self.first_touch_at.isoformat(),
                "last_touch_at": None if self.last_touch_at is None else self.last_touch_at.isoformat(),
                "contact_count": self.contact_count,
                "fill_percentage": str(self.fill_percentage),
                "maximum_penetration": str(self.maximum_penetration),
                "age_bars": self.age_bars,
            }
        )

    def to_state(self) -> Mapping[str, Any]:
        return self.to_value()

    @classmethod
    def from_state(cls, payload: Mapping[str, object]) -> "FvgZoneLifecycle":
        payload = _require_canonical_mapping_keys(payload, "lifecycle state")
        required = {
            "zone_id",
            "status",
            "detected_at",
            "last_transition_at",
            "last_observed_at",
            "first_touch_at",
            "last_touch_at",
            "contact_count",
            "fill_percentage",
            "maximum_penetration",
            "age_bars",
        }
        if set(payload) != required:
            raise ValueError("invalid lifecycle state keys")
        string_fields = {
            "zone_id",
            "status",
            "detected_at",
            "last_transition_at",
            "last_observed_at",
            "fill_percentage",
            "maximum_penetration",
        }
        for name in string_fields:
            if type(payload[name]) is not str:
                raise ValueError(f"{name} must be a canonical string")
        for name in ("first_touch_at", "last_touch_at"):
            if payload[name] is not None and type(payload[name]) is not str:
                raise ValueError(f"{name} must be a canonical string or null")
        for name in ("contact_count", "age_bars"):
            if type(payload[name]) is not int:
                raise ValueError(f"{name} must be a canonical int")
        fill_percentage = parse_canonical_decimal_text(payload["fill_percentage"], "lifecycle fill_percentage")
        maximum_penetration = parse_canonical_decimal_text(
            payload["maximum_penetration"], "lifecycle maximum_penetration"
        )
        return cls(
            zone_id=payload["zone_id"],
            status=FvgLifecycleStatus(payload["status"]),
            detected_at=parse_canonical_utc_datetime(payload["detected_at"], "detected_at"),
            last_transition_at=parse_canonical_utc_datetime(payload["last_transition_at"], "last_transition_at"),
            last_observed_at=parse_canonical_utc_datetime(payload["last_observed_at"], "last_observed_at"),
            first_touch_at=(
                None
                if payload["first_touch_at"] is None
                else parse_canonical_utc_datetime(payload["first_touch_at"], "first_touch_at")
            ),
            last_touch_at=(
                None
                if payload["last_touch_at"] is None
                else parse_canonical_utc_datetime(payload["last_touch_at"], "last_touch_at")
            ),
            contact_count=payload["contact_count"],
            fill_percentage=fill_percentage,
            maximum_penetration=maximum_penetration,
            age_bars=payload["age_bars"],
        )


def initial_lifecycle(zone: FvgZone) -> FvgZoneLifecycle:
    if not isinstance(zone, FvgZone):
        raise TypeError("zone must be an FvgZone")
    return FvgZoneLifecycle(
        zone_id=zone.zone_id,
        status=FvgLifecycleStatus.ACTIVE,
        detected_at=zone.created_at,
        last_transition_at=zone.created_at,
        last_observed_at=zone.created_at,
    )


def _zone_fill_fraction_values(
    zone: FvgZone,
    *,
    bar_low: Decimal,
    bar_high: Decimal,
) -> tuple[bool, Decimal]:
    try:
        if zone.direction is FvgDirection.BULLISH:
            touched = bar_low <= zone.upper
            penetration = (zone.upper - bar_low) / zone.size if touched else _ZERO
        else:
            touched = bar_high >= zone.lower
            penetration = (bar_high - zone.lower) / zone.size if touched else _ZERO
        return touched, min(_ONE, max(_ZERO, penetration))
    except DecimalException as exc:
        raise ValueError("lifecycle arithmetic exceeds supported decimal range") from exc


@deterministic_decimal
def zone_fill_fraction(zone: FvgZone, bar: CandleLike) -> tuple[bool, Decimal]:
    if not isinstance(zone, FvgZone):
        raise TypeError("zone must be an FvgZone")
    _validate_candle(bar, "lifecycle_bar")
    if bar.symbol != zone.symbol or bar.timeframe != zone.timeframe:
        raise ValueError("lifecycle bar scope differs from zone")
    return _zone_fill_fraction_values(
        zone,
        bar_low=as_decimal(bar.low),
        bar_high=as_decimal(bar.high),
    )


def _observe_lifecycle_validated(
    zone: FvgZone,
    lifecycle: FvgZoneLifecycle,
    *,
    bar_close: datetime,
    duration_seconds: int,
    bar_low: Decimal,
    bar_high: Decimal,
) -> FvgZoneLifecycle:
    if lifecycle.status in {
        FvgLifecycleStatus.INVALIDATED,
        FvgLifecycleStatus.EXPIRED,
        FvgLifecycleStatus.ARCHIVED,
    }:
        raise ValueError("terminal lifecycle cannot be observed again")
    if bar_close == lifecycle.last_observed_at:
        if bar_close == lifecycle.detected_at and lifecycle.age_bars == 0:
            return lifecycle
        touched, penetration = _zone_fill_fraction_values(
            zone,
            bar_low=bar_low,
            bar_high=bar_high,
        )
        recorded_touch = lifecycle.last_touch_at == bar_close
        if touched != recorded_touch or (touched and lifecycle.fill_percentage != penetration):
            raise ValueError("conflicting duplicate lifecycle observation")
        return lifecycle
    if bar_close < lifecycle.last_observed_at:
        raise ValueError("lifecycle observation regressed in time")
    elapsed = bar_close - lifecycle.last_observed_at
    elapsed_seconds = elapsed.days * 86400 + elapsed.seconds
    if elapsed.microseconds or elapsed_seconds % duration_seconds:
        raise ValueError("lifecycle observation must align to timeframe slots")
    elapsed_slots = elapsed_seconds // duration_seconds
    touched, penetration = _zone_fill_fraction_values(
        zone,
        bar_low=bar_low,
        bar_high=bar_high,
    )
    age = lifecycle.age_bars + elapsed_slots
    if not touched:
        return FvgZoneLifecycle(
            zone_id=lifecycle.zone_id,
            status=lifecycle.status,
            detected_at=lifecycle.detected_at,
            last_transition_at=lifecycle.last_transition_at,
            last_observed_at=bar_close,
            first_touch_at=lifecycle.first_touch_at,
            last_touch_at=lifecycle.last_touch_at,
            contact_count=lifecycle.contact_count,
            fill_percentage=_ZERO,
            maximum_penetration=lifecycle.maximum_penetration,
            age_bars=age,
        )
    maximum = max(lifecycle.maximum_penetration, penetration)
    status = (
        FvgLifecycleStatus.FULLY_FILLED
        if maximum >= _ONE
        else (FvgLifecycleStatus.PARTIALLY_FILLED if maximum > _ZERO else lifecycle.status)
    )
    first_touch = lifecycle.first_touch_at or bar_close
    transition_time = bar_close if status is not lifecycle.status else lifecycle.last_transition_at
    return FvgZoneLifecycle(
        zone_id=zone.zone_id,
        status=status,
        detected_at=lifecycle.detected_at,
        last_transition_at=transition_time,
        last_observed_at=bar_close,
        first_touch_at=first_touch,
        last_touch_at=bar_close,
        contact_count=lifecycle.contact_count + 1,
        fill_percentage=penetration,
        maximum_penetration=maximum,
        age_bars=age,
    )


@deterministic_decimal
def observe_lifecycle(
    zone: FvgZone,
    lifecycle: FvgZoneLifecycle,
    bar: CandleLike,
) -> FvgZoneLifecycle:
    if not isinstance(zone, FvgZone):
        raise TypeError("zone must be an FvgZone")
    if not isinstance(lifecycle, FvgZoneLifecycle):
        raise TypeError("lifecycle must be an FvgZoneLifecycle")
    if lifecycle.zone_id != zone.zone_id:
        raise ValueError("lifecycle does not belong to zone")
    _validate_candle(bar, "lifecycle_bar")
    if bar.symbol != zone.symbol or bar.timeframe != zone.timeframe:
        raise ValueError("lifecycle bar scope differs from zone")
    return _observe_lifecycle_validated(
        zone,
        lifecycle,
        bar_close=as_utc(bar.close_time, "bar.close_time"),
        duration_seconds=timeframe_seconds(zone.timeframe),
        bar_low=as_decimal(bar.low),
        bar_high=as_decimal(bar.high),
    )


@deterministic_decimal
def terminal_lifecycle(
    lifecycle: FvgZoneLifecycle,
    status: FvgLifecycleStatus,
    event_time: datetime,
) -> FvgZoneLifecycle:
    if not isinstance(lifecycle, FvgZoneLifecycle):
        raise TypeError("lifecycle must be an FvgZoneLifecycle")
    if not isinstance(status, FvgLifecycleStatus):
        raise TypeError("status must be an FvgLifecycleStatus")
    if status not in {
        FvgLifecycleStatus.INVALIDATED,
        FvgLifecycleStatus.EXPIRED,
        FvgLifecycleStatus.ARCHIVED,
        FvgLifecycleStatus.FULLY_FILLED,
    }:
        raise ValueError("terminal lifecycle requires a terminal status")
    instant = as_utc(event_time)
    if instant != lifecycle.last_observed_at:
        raise ValueError("terminal event must equal the latest lifecycle observation")
    if lifecycle.status in {
        FvgLifecycleStatus.INVALIDATED,
        FvgLifecycleStatus.EXPIRED,
        FvgLifecycleStatus.ARCHIVED,
    }:
        if lifecycle.status is status:
            return lifecycle
        raise ValueError("terminal lifecycle status cannot be rewritten")
    return FvgZoneLifecycle(
        zone_id=lifecycle.zone_id,
        status=status,
        detected_at=lifecycle.detected_at,
        last_transition_at=instant,
        last_observed_at=instant,
        first_touch_at=lifecycle.first_touch_at,
        last_touch_at=lifecycle.last_touch_at,
        contact_count=lifecycle.contact_count,
        fill_percentage=lifecycle.fill_percentage,
        maximum_penetration=lifecycle.maximum_penetration,
        age_bars=lifecycle.age_bars,
    )
