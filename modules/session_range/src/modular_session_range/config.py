from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_SESSION_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
_MAX_SESSION_SPECS = 64


class SessionRangeConfigurationError(ValueError):
    """Raised when Session Range configuration is invalid."""


class SessionBoundaryError(ValueError):
    """Raised when a local session boundary cannot be represented safely."""


def _require_string(value: object, field_name: str) -> str:
    if not isinstance(value, str):
        raise SessionRangeConfigurationError(f"{field_name} must be a string")
    return value


def _parse_time(value: str, field_name: str) -> time:
    if not isinstance(value, str):
        raise SessionRangeConfigurationError(f"{field_name} must be a string in HH:MM[:SS] format")
    parts = value.split(":")
    if len(parts) not in {2, 3}:
        raise SessionRangeConfigurationError(f"{field_name} must use HH:MM or HH:MM:SS")
    try:
        hour, minute = int(parts[0]), int(parts[1])
        second = int(parts[2]) if len(parts) == 3 else 0
        return time(hour=hour, minute=minute, second=second)
    except (ValueError, TypeError) as exc:
        raise SessionRangeConfigurationError(f"invalid {field_name}: {value!r}") from exc


def _decimal(value: object, field_name: str) -> Decimal:
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise SessionRangeConfigurationError(
            f"{field_name} must be a finite decimal number"
        ) from exc
    if not result.is_finite():
        raise SessionRangeConfigurationError(f"{field_name} must be finite")
    return result


def _resolve_local(naive: datetime, tz: ZoneInfo, fold: int, field_name: str) -> datetime:
    candidates: list[datetime] = []
    for candidate_fold in (0, 1):
        candidate = naive.replace(tzinfo=tz, fold=candidate_fold)
        round_trip = candidate.astimezone(UTC).astimezone(tz)
        if round_trip.replace(tzinfo=None) == naive:
            candidates.append(candidate)
    unique: dict[timedelta | None, datetime] = {item.utcoffset(): item for item in candidates}
    if not unique:
        raise SessionBoundaryError(
            f"{field_name} {naive.isoformat()} is a nonexistent local time in {tz.key}"
        )
    ordered = sorted(unique.values(), key=lambda item: item.astimezone(UTC))
    if len(ordered) == 1:
        return ordered[0]
    return ordered[fold]


@dataclass(frozen=True, slots=True)
class SessionRangeSpec:
    name: str
    timezone_name: str
    start: time
    end: time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)
    minimum_bars: int = 1
    minimum_range: Decimal = Decimal("0")
    maximum_range: Decimal | None = None
    emit_active: bool = True
    dst_fold: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not _SESSION_NAME_RE.fullmatch(self.name):
            raise SessionRangeConfigurationError(
                "session name must match [A-Za-z0-9][A-Za-z0-9_.-]{0,63}"
            )
        if not isinstance(self.timezone_name, str):
            raise SessionRangeConfigurationError("timezone_name must be a string")
        if not isinstance(self.start, time) or not isinstance(self.end, time):
            raise SessionRangeConfigurationError("start and end must be datetime.time values")
        if self.start.tzinfo is not None or self.end.tzinfo is not None:
            raise SessionRangeConfigurationError(
                "start and end must be naive clock values; timezone is configured separately"
            )
        try:
            ZoneInfo(self.timezone_name)
        except ZoneInfoNotFoundError as exc:
            raise SessionRangeConfigurationError(
                f"unknown IANA timezone: {self.timezone_name!r}"
            ) from exc
        if (
            not isinstance(self.weekdays, tuple)
            or not self.weekdays
            or any(isinstance(day, bool) or not isinstance(day, int) for day in self.weekdays)
            or len(set(self.weekdays)) != len(self.weekdays)
        ):
            raise SessionRangeConfigurationError(
                "weekdays must be a non-empty tuple of integers without duplicates"
            )
        if any(day < 0 or day > 6 for day in self.weekdays):
            raise SessionRangeConfigurationError("weekdays must contain integers in [0, 6]")
        if isinstance(self.minimum_bars, bool) or not isinstance(self.minimum_bars, int):
            raise SessionRangeConfigurationError("minimum_bars must be an integer")
        if self.minimum_bars < 1:
            raise SessionRangeConfigurationError("minimum_bars must be at least 1")
        if not isinstance(self.minimum_range, Decimal) or not self.minimum_range.is_finite():
            raise SessionRangeConfigurationError("minimum_range must be a finite Decimal")
        if self.minimum_range < 0:
            raise SessionRangeConfigurationError("minimum_range cannot be negative")
        if self.maximum_range is not None and (
            not isinstance(self.maximum_range, Decimal) or not self.maximum_range.is_finite()
        ):
            raise SessionRangeConfigurationError("maximum_range must be a finite Decimal or None")
        if self.maximum_range is not None and self.maximum_range <= self.minimum_range:
            raise SessionRangeConfigurationError("maximum_range must be greater than minimum_range")
        if not isinstance(self.emit_active, bool):
            raise SessionRangeConfigurationError("emit_active must be boolean")
        if isinstance(self.dst_fold, bool) or not isinstance(self.dst_fold, int):
            raise SessionRangeConfigurationError("dst_fold must be an integer")
        if self.dst_fold not in {0, 1}:
            raise SessionRangeConfigurationError("dst_fold must be 0 or 1")
        object.__setattr__(self, "weekdays", tuple(sorted(self.weekdays)))

    @property
    def timezone(self) -> ZoneInfo:
        return ZoneInfo(self.timezone_name)

    def bounds_for(self, anchor_date: date) -> tuple[datetime, datetime]:
        if anchor_date.weekday() not in self.weekdays:
            raise SessionBoundaryError(
                f"session {self.name!r} is disabled for weekday {anchor_date.weekday()}"
            )
        end_date = anchor_date if self.end > self.start else anchor_date + timedelta(days=1)
        start_naive = datetime.combine(anchor_date, self.start)
        end_naive = datetime.combine(end_date, self.end)
        start_local = _resolve_local(start_naive, self.timezone, self.dst_fold, "session start")
        end_local = _resolve_local(end_naive, self.timezone, self.dst_fold, "session end")
        start_utc = start_local.astimezone(UTC)
        end_utc = end_local.astimezone(UTC)
        if end_utc <= start_utc:
            raise SessionBoundaryError(f"session {self.name!r} has non-positive UTC duration")
        return start_utc, end_utc

    def next_start_after(self, anchor_date: date) -> datetime:
        for offset in range(1, 15):
            candidate = anchor_date + timedelta(days=offset)
            if candidate.weekday() not in self.weekdays:
                continue
            try:
                start, _ = self.bounds_for(candidate)
            except SessionBoundaryError:
                continue
            return start
        raise SessionBoundaryError(
            f"cannot resolve next valid start for session {self.name!r} within 14 days"
        )

    def to_mapping(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "name": self.name,
                "timezone": self.timezone_name,
                "start": self.start.isoformat(),
                "end": self.end.isoformat(),
                "weekdays": self.weekdays,
                "minimum_bars": self.minimum_bars,
                "minimum_range": str(self.minimum_range),
                "maximum_range": None if self.maximum_range is None else str(self.maximum_range),
                "emit_active": self.emit_active,
                "dst_fold": self.dst_fold,
            }
        )

    @classmethod
    def from_mapping(cls, payload: Mapping[str, Any]) -> SessionRangeSpec:
        allowed = {
            "name",
            "timezone",
            "start",
            "end",
            "weekdays",
            "minimum_bars",
            "minimum_range",
            "maximum_range",
            "emit_active",
            "dst_fold",
        }
        unknown = sorted(set(payload) - allowed)
        if unknown:
            raise SessionRangeConfigurationError(
                f"unknown session keys for {payload.get('name', '<unnamed>')}: {', '.join(unknown)}"
            )
        required = ("name", "timezone", "start", "end")
        missing = [key for key in required if key not in payload]
        if missing:
            raise SessionRangeConfigurationError(
                f"missing required session keys: {', '.join(missing)}"
            )
        weekdays_raw = payload.get("weekdays", (0, 1, 2, 3, 4, 5, 6))
        if isinstance(weekdays_raw, (str, bytes)) or not isinstance(weekdays_raw, Sequence):
            raise SessionRangeConfigurationError("weekdays must be an array")
        weekdays: list[int] = []
        for item in weekdays_raw:
            if isinstance(item, bool) or not isinstance(item, int):
                raise SessionRangeConfigurationError("weekdays must contain integers")
            weekdays.append(item)
        minimum_bars = payload.get("minimum_bars", 1)
        if isinstance(minimum_bars, bool) or not isinstance(minimum_bars, int):
            raise SessionRangeConfigurationError("minimum_bars must be an integer")
        emit_active = payload.get("emit_active", True)
        if not isinstance(emit_active, bool):
            raise SessionRangeConfigurationError("emit_active must be boolean")
        dst_fold = payload.get("dst_fold", 0)
        if isinstance(dst_fold, bool) or not isinstance(dst_fold, int):
            raise SessionRangeConfigurationError("dst_fold must be an integer")
        maximum_raw = payload.get("maximum_range")
        return cls(
            name=_require_string(payload["name"], "name"),
            timezone_name=_require_string(payload["timezone"], "timezone"),
            start=_parse_time(_require_string(payload["start"], "start"), "start"),
            end=_parse_time(_require_string(payload["end"], "end"), "end"),
            weekdays=tuple(weekdays),
            minimum_bars=minimum_bars,
            minimum_range=_decimal(payload.get("minimum_range", "0"), "minimum_range"),
            maximum_range=(None if maximum_raw is None else _decimal(maximum_raw, "maximum_range")),
            emit_active=emit_active,
            dst_fold=dst_fold,
        )


def parse_specs(rows: Sequence[Mapping[str, Any]]) -> tuple[SessionRangeSpec, ...]:
    if isinstance(rows, (str, bytes)) or not isinstance(rows, Sequence):
        raise SessionRangeConfigurationError("sessions must be an array of objects")
    if len(rows) > _MAX_SESSION_SPECS:
        raise SessionRangeConfigurationError(
            f"sessions cannot contain more than {_MAX_SESSION_SPECS} definitions"
        )
    specs: list[SessionRangeSpec] = []
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise SessionRangeConfigurationError(f"sessions[{index}] must be an object")
        specs.append(SessionRangeSpec.from_mapping(row))
    if not specs:
        raise SessionRangeConfigurationError("at least one session definition is required")
    names = [spec.name for spec in specs]
    if len(names) != len(set(names)):
        raise SessionRangeConfigurationError("session names must be unique")
    return tuple(specs)
