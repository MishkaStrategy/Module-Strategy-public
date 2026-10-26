from __future__ import annotations

from datetime import datetime, time, timedelta, timezone

import pytest

from modular_core import Candle
from modular_core.sessions import (
    BarBoundary,
    SessionDefinition,
    SessionView,
    TradingCalendar,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _session(**overrides: object) -> SessionDefinition:
    values: dict[str, object] = {
        "name": "ny",
        "timezone": "America/New_York",
        "start": time(9, 30),
        "end": time(16, 0),
        "weekdays": (0, 1, 2, 3, 4),
    }
    values.update(overrides)
    return SessionDefinition(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 1, None, object()])
def test_session_definition_requires_string_name(value: object) -> None:
    with pytest.raises(TypeError, match="session name must be str"):
        _session(name=value)


@pytest.mark.parametrize("value", ["", " ", " ny", "ny "])
def test_session_definition_requires_canonical_name(value: str) -> None:
    with pytest.raises(ValueError):
        _session(name=value)


@pytest.mark.parametrize("value", [True, 1, None, object()])
def test_session_definition_requires_string_timezone(value: object) -> None:
    with pytest.raises(TypeError, match="session timezone must be str"):
        _session(timezone=value)


@pytest.mark.parametrize("field", ["start", "end"])
@pytest.mark.parametrize("value", [None, "09:30", 930, object()])
def test_session_definition_requires_time_values(field: str, value: object) -> None:
    with pytest.raises(TypeError, match=f"session {field} must be time"):
        _session(**{field: value})


@pytest.mark.parametrize("field", ["start", "end"])
def test_session_definition_rejects_timezone_aware_local_times(field: str) -> None:
    with pytest.raises(ValueError, match="timezone-naive"):
        _session(**{field: time(9, 30, tzinfo=timezone.utc)})


def test_session_definition_freezes_detached_weekdays() -> None:
    weekdays = [4, 0, 2]
    session = _session(weekdays=weekdays)
    weekdays.clear()
    assert session.weekdays == (4, 0, 2)


@pytest.mark.parametrize("value", ["01234", iter((0, 1)), 1, object()])
def test_session_definition_requires_weekday_sequence(value: object) -> None:
    with pytest.raises(TypeError, match="weekdays must be a sequence"):
        _session(weekdays=value)


@pytest.mark.parametrize("value", [(True,), (1.0,), ("1",)])
def test_session_definition_requires_integer_weekdays(value: object) -> None:
    with pytest.raises(TypeError, match="weekdays must contain integers"):
        _session(weekdays=value)


@pytest.mark.parametrize("value", [(-1,), (7,)])
def test_session_definition_rejects_out_of_range_weekdays(value: object) -> None:
    with pytest.raises(ValueError, match=r"\[0, 6\]"):
        _session(weekdays=value)


def test_session_definition_rejects_duplicate_weekdays() -> None:
    with pytest.raises(ValueError, match="duplicates"):
        _session(weekdays=(0, 0))


def test_session_bounds_require_date_not_datetime() -> None:
    session = _session()
    with pytest.raises(TypeError, match="local_date must be date"):
        session.bounds_for(NOW)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="local_date must be date"):
        session.bounds_for("2026-01-01")  # type: ignore[arg-type]


def test_session_contains_requires_aware_datetime() -> None:
    session = _session()
    with pytest.raises(TypeError, match="session timestamp must be datetime"):
        session.contains("2026-01-01")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        session.contains(datetime(2026, 1, 1))


def test_session_contains_normalizes_offset_timestamp() -> None:
    session = SessionDefinition(
        "utc",
        "UTC",
        time(0, 0),
        time(1, 0),
        weekdays=(3,),
    )
    assert session.contains(datetime(2026, 1, 1, 3, 30, tzinfo=timezone(timedelta(hours=3))))


def test_bar_boundary_normalizes_and_validates_times() -> None:
    boundary = BarBoundary(
        "5m",
        datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3))),
        datetime(2026, 1, 1, 3, 5, tzinfo=timezone(timedelta(hours=3))),
    )
    assert boundary.open_time == NOW
    assert boundary.close_time == NOW + timedelta(minutes=5)

    with pytest.raises(ValueError, match="after open_time"):
        BarBoundary("5m", NOW, NOW)
    with pytest.raises(ValueError, match="surrounding whitespace"):
        BarBoundary(" 5m", NOW, NOW + timedelta(minutes=5))


def test_trading_calendar_freezes_and_validates_sessions() -> None:
    session = _session()
    sessions = [session]
    calendar = TradingCalendar(sessions)
    sessions.clear()
    assert calendar.sessions == {"ny": session}
    with pytest.raises(TypeError, match="sessions must be a sequence"):
        TradingCalendar(iter((session,)))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="contain SessionDefinition"):
        TradingCalendar((object(),))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="unique"):
        TradingCalendar((session, session))


def test_trading_calendar_public_boundaries() -> None:
    session = SessionDefinition("utc", "UTC", time(0), time(1))
    calendar = TradingCalendar((session,))
    assert calendar.is_open("utc", NOW)
    with pytest.raises(ValueError, match="surrounding whitespace"):
        calendar.is_open(" utc", NOW)
    with pytest.raises(TypeError, match="candle must be Candle"):
        calendar.boundary(object())  # type: ignore[arg-type]

    candle = Candle(NOW, 100, 101, 99, 100, 1, "BTCUSDT", "5m")
    boundary = calendar.boundary(candle)
    assert boundary == BarBoundary("5m", NOW, NOW + timedelta(minutes=5))


def test_session_view_validates_and_normalizes_snapshot() -> None:
    calendar = TradingCalendar()
    view = SessionView(
        calendar,
        datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3))),
    )
    assert view.timestamp == NOW
    with pytest.raises(TypeError, match="calendar must be TradingCalendar"):
        SessionView(object(), NOW)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="timezone-aware"):
        SessionView(calendar, datetime(2026, 1, 1))
