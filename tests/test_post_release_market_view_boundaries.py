from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core.models import Candle
from modular_core.module_api import MarketView


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bar(index: int = 0, *, timeframe: str = "5m") -> Candle:
    return Candle(
        timestamp=NOW + timedelta(minutes=5 * index),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe=timeframe,
    )


def _view() -> MarketView:
    return MarketView(
        base_bars=(_bar(0), _bar(1)),
        timeframe_bars={"15m": (_bar(0, timeframe="15m"),)},
        index=1,
        cash=1_000.0,
        equity=1_000.0,
        position=None,
        pending_orders=(),
    )


@pytest.mark.parametrize("value", [True, 1.0, "1", object()])
def test_history_rejects_non_integer_lengths(value: object) -> None:
    with pytest.raises(TypeError, match="length must be int"):
        _view().history(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [1, "yes", None, object()])
def test_history_rejects_non_boolean_include_current(value: object) -> None:
    with pytest.raises(TypeError, match="include_current must be bool"):
        _view().history(1, include_current=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", [True, 1, object()])
def test_history_rejects_non_string_timeframe(value: object) -> None:
    with pytest.raises(TypeError, match="timeframe must be str"):
        _view().history(1, timeframe=value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "   "])
def test_history_rejects_blank_timeframe(value: str) -> None:
    with pytest.raises(ValueError, match="timeframe is required"):
        _view().history(1, timeframe=value)


@pytest.mark.parametrize("value", [True, 1, object()])
def test_current_for_rejects_non_string_timeframe(value: object) -> None:
    with pytest.raises(TypeError, match="timeframe must be str"):
        _view().current_for(value)  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["", "   "])
def test_current_for_rejects_blank_timeframe(value: str) -> None:
    with pytest.raises(ValueError, match="timeframe is required"):
        _view().current_for(value)


def test_history_preserves_existing_nonpositive_and_unknown_behavior() -> None:
    view = _view()

    assert view.history(0) == ()
    assert view.history(-1) == ()
    assert view.history(2, include_current=False) == (_bar(0),)
    assert view.history(2, timeframe="unknown") == ()


def test_current_for_unknown_timeframe_returns_none() -> None:
    assert _view().current_for("unknown") is None
