from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from .models import Candle
from .validation import require_nonblank, utc_datetime


@dataclass(frozen=True, slots=True)
class SessionDefinition:
    name: str
    timezone: str
    start: time
    end: time
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4, 5, 6)

    def __post_init__(self) -> None:
        name = _canonical_string(self.name, "session name")
        timezone_name = _canonical_string(self.timezone, "session timezone")
        ZoneInfo(timezone_name)
        _require_local_time(self.start, "session start")
        _require_local_time(self.end, "session end")
        weekdays = _freeze_weekdays(self.weekdays)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "timezone", timezone_name)
        object.__setattr__(self, "weekdays", weekdays)

    def bounds_for(self, local_date: date) -> tuple[datetime, datetime]:
        if not isinstance(local_date, date) or isinstance(local_date, datetime):
            raise TypeError("local_date must be date")
        tz = ZoneInfo(self.timezone)
        start = datetime.combine(local_date, self.start, tzinfo=tz)
        end_date = local_date if self.end > self.start else local_date + timedelta(days=1)
        end = datetime.combine(end_date, self.end, tzinfo=tz)
        return start, end

    def contains(self, timestamp: datetime) -> bool:
        timestamp = utc_datetime(timestamp, "session timestamp")
        local = timestamp.astimezone(ZoneInfo(self.timezone))
        candidate_dates = (local.date(), local.date() - timedelta(days=1))
        for candidate in candidate_dates:
            if candidate.weekday() not in self.weekdays:
                continue
            start, end = self.bounds_for(candidate)
            if start <= local < end:
                return True
        return False


@dataclass(frozen=True, slots=True)
class BarBoundary:
    timeframe: str
    open_time: datetime
    close_time: datetime

    def __post_init__(self) -> None:
        timeframe = _canonical_string(self.timeframe, "boundary timeframe")
        open_time = utc_datetime(self.open_time, "boundary open_time")
        close_time = utc_datetime(self.close_time, "boundary close_time")
        if close_time <= open_time:
            raise ValueError("boundary close_time must be after open_time")
        object.__setattr__(self, "timeframe", timeframe)
        object.__setattr__(self, "open_time", open_time)
        object.__setattr__(self, "close_time", close_time)


class TradingCalendar:
    def __init__(self, sessions: Sequence[SessionDefinition] = ()) -> None:
        if isinstance(sessions, (str, bytes)) or not isinstance(sessions, Sequence):
            raise TypeError("sessions must be a sequence")
        frozen = tuple(sessions)
        if any(not isinstance(session, SessionDefinition) for session in frozen):
            raise TypeError("sessions must contain SessionDefinition")
        names = [session.name for session in frozen]
        if len(names) != len(set(names)):
            raise ValueError("session names must be unique")
        self._sessions = MappingProxyType({session.name: session for session in frozen})

    @property
    def sessions(self) -> Mapping[str, SessionDefinition]:
        return self._sessions

    def is_open(self, session_name: str, timestamp: datetime) -> bool:
        session_name = _canonical_string(session_name, "session name")
        return self._sessions[session_name].contains(timestamp)

    def boundary(self, candle: Candle) -> BarBoundary:
        if not isinstance(candle, Candle):
            raise TypeError("candle must be Candle")
        return BarBoundary(candle.timeframe, candle.open_time, candle.close_time)


@dataclass(frozen=True, slots=True)
class SessionView:
    calendar: TradingCalendar
    timestamp: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.calendar, TradingCalendar):
            raise TypeError("calendar must be TradingCalendar")
        object.__setattr__(
            self,
            "timestamp",
            utc_datetime(self.timestamp, "session view timestamp"),
        )

    def is_open(self, name: str) -> bool:
        return self.calendar.is_open(name, self.timestamp)


def _canonical_string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be str")
    require_nonblank(value, name)
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    return value


def _require_local_time(value: object, name: str) -> None:
    if not isinstance(value, time):
        raise TypeError(f"{name} must be time")
    if value.tzinfo is not None and value.utcoffset() is not None:
        raise ValueError(f"{name} must be timezone-naive")


def _freeze_weekdays(value: object) -> tuple[int, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("weekdays must be a sequence")
    result: list[int] = []
    for day in value:
        if not isinstance(day, int) or isinstance(day, bool):
            raise TypeError("weekdays must contain integers")
        if day < 0 or day > 6:
            raise ValueError("weekdays must be in [0, 6]")
        result.append(day)
    if len(result) != len(set(result)):
        raise ValueError("weekdays contains duplicates")
    return tuple(result)
