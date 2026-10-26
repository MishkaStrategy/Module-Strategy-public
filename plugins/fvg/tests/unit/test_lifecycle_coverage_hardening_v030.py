from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from conftest import bullish_triplet, candle
from modular_strategy_fvg import FvgConfig
from modular_strategy_fvg.detector import detect_zone
from modular_strategy_fvg.lifecycle import (
    FvgZoneLifecycle,
    _observe_lifecycle_validated,
    _zone_fill_fraction_values,
    initial_lifecycle,
    observe_lifecycle,
    terminal_lifecycle,
    zone_fill_fraction,
)
from modular_strategy_fvg.models import FvgDirection, FvgLifecycleStatus


def _zone():
    a, b, c = bullish_triplet()
    zone = detect_zone(a, b, c, FvgConfig(timeframes=("15m",)))
    assert zone is not None
    return zone


def _active() -> FvgZoneLifecycle:
    return initial_lifecycle(_zone())


def test_initial_lifecycle_round_trip_and_immutability() -> None:
    lifecycle = _active()
    assert lifecycle.status is FvgLifecycleStatus.ACTIVE
    assert lifecycle.detected_at == _zone().created_at
    assert FvgZoneLifecycle.from_state(dict(lifecycle.to_state())) == lifecycle
    with pytest.raises(TypeError):
        lifecycle.to_value()["status"] = "expired"  # type: ignore[index]
    with pytest.raises(TypeError):
        initial_lifecycle(object())  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "changes",
    [
        {"zone_id": 1},
        {"zone_id": ""},
        {"status": "active"},
        {"detected_at": "bad"},
        {"last_transition_at": _zone().created_at - timedelta(seconds=1)},
        {"last_observed_at": _zone().created_at - timedelta(seconds=1)},
        {"contact_count": 1.0},
        {"contact_count": -1},
        {"age_bars": 1.0},
        {"age_bars": -1},
        {"fill_percentage": 1},
        {"fill_percentage": Decimal("NaN")},
        {"fill_percentage": Decimal("1.1")},
        {"maximum_penetration": Decimal("-1")},
        {"fill_percentage": Decimal("0.5")},
        {"first_touch_at": _zone().created_at},
        {"contact_count": 1},
        {
            "contact_count": 1,
            "age_bars": 1,
            "first_touch_at": _zone().created_at,
            "last_touch_at": _zone().created_at + timedelta(minutes=15),
            "last_observed_at": _zone().created_at + timedelta(minutes=15),
        },
        {
            "contact_count": 2,
            "age_bars": 2,
            "first_touch_at": _zone().created_at + timedelta(minutes=15),
            "last_touch_at": _zone().created_at + timedelta(minutes=15),
            "last_observed_at": _zone().created_at + timedelta(minutes=30),
        },
        {"contact_count": 1, "age_bars": 0},
        {"last_observed_at": _zone().created_at + timedelta(minutes=15)},
        {
            "status": FvgLifecycleStatus.EXPIRED,
            "last_observed_at": _zone().created_at + timedelta(minutes=15),
        },
        {"maximum_penetration": Decimal("0.1")},
        {
            "status": FvgLifecycleStatus.PARTIALLY_FILLED,
            "contact_count": 1,
            "age_bars": 1,
            "first_touch_at": _zone().created_at + timedelta(minutes=15),
            "last_touch_at": _zone().created_at + timedelta(minutes=15),
            "last_observed_at": _zone().created_at + timedelta(minutes=15),
            "last_transition_at": _zone().created_at,
            "fill_percentage": Decimal("0.5"),
            "maximum_penetration": Decimal("0.5"),
        },
        {
            "status": FvgLifecycleStatus.FULLY_FILLED,
            "contact_count": 0,
            "age_bars": 1,
            "last_observed_at": _zone().created_at + timedelta(minutes=15),
            "last_transition_at": _zone().created_at + timedelta(minutes=15),
            "fill_percentage": Decimal("1"),
            "maximum_penetration": Decimal("1"),
        },
    ],
)
def test_lifecycle_rejects_invalid_contracts(changes: dict[str, object]) -> None:
    with pytest.raises((TypeError, ValueError)):
        replace(_active(), **changes)


@pytest.mark.parametrize(
    "mutation",
    ["missing", "string", "touch", "count", "decimal", "status", "datetime"],
)
def test_lifecycle_from_state_rejects_malformed_payload(mutation: str) -> None:
    state = dict(_active().to_state())
    if mutation == "missing":
        state.pop("zone_id")
    elif mutation == "string":
        state["zone_id"] = 1
    elif mutation == "touch":
        state["first_touch_at"] = 1
    elif mutation == "count":
        state["contact_count"] = True
    elif mutation == "decimal":
        state["fill_percentage"] = 1
    elif mutation == "status":
        state["status"] = "unknown"
    else:
        state["detected_at"] = "not-a-date"
    with pytest.raises((TypeError, ValueError)):
        FvgZoneLifecycle.from_state(state)


def test_zone_fill_fraction_bullish_and_bearish() -> None:
    zone = _zone()
    assert _zone_fill_fraction_values(zone, bar_low=Decimal("106"), bar_high=Decimal("110")) == (
        False,
        Decimal("0"),
    )
    touched, partial = _zone_fill_fraction_values(
        zone,
        bar_low=Decimal("102"),
        bar_high=Decimal("110"),
    )
    assert touched and partial == Decimal("0.6")
    assert _zone_fill_fraction_values(zone, bar_low=Decimal("90"), bar_high=Decimal("110"))[1] == Decimal("1")

    bearish = replace(zone, direction=FvgDirection.BEARISH)
    assert _zone_fill_fraction_values(
        bearish,
        bar_low=Decimal("90"),
        bar_high=Decimal("99"),
    ) == (False, Decimal("0"))
    assert _zone_fill_fraction_values(
        bearish,
        bar_low=Decimal("90"),
        bar_high=Decimal("102"),
    )[1] == Decimal("0.4")


def test_zone_fill_fraction_validates_scope_and_types() -> None:
    zone = _zone()
    bar = candle(3, open_=107, high=110, low=102, close=108)
    assert zone_fill_fraction(zone, bar) == (True, Decimal("0.6"))
    with pytest.raises(TypeError):
        zone_fill_fraction(object(), bar)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="scope"):
        zone_fill_fraction(zone, replace(bar, symbol="ETHUSDT"))


def test_observe_lifecycle_untouched_partial_full_and_duplicates() -> None:
    zone = _zone()
    active = _active()
    untouched_bar = candle(3, open_=107, high=110, low=106, close=108)
    untouched = observe_lifecycle(zone, active, untouched_bar)
    assert untouched.status is FvgLifecycleStatus.ACTIVE
    assert untouched.age_bars == 1
    assert untouched.contact_count == 0

    partial_bar = candle(4, open_=107, high=110, low=102, close=108)
    partial = observe_lifecycle(zone, untouched, partial_bar)
    assert partial.status is FvgLifecycleStatus.PARTIALLY_FILLED
    assert partial.fill_percentage == Decimal("0.6")
    assert partial.maximum_penetration == Decimal("0.6")
    assert partial.contact_count == 1

    assert observe_lifecycle(zone, partial, partial_bar) == partial
    with pytest.raises(ValueError, match="conflicting duplicate"):
        observe_lifecycle(zone, partial, replace(partial_bar, low=101))

    no_touch_bar = candle(5, open_=107, high=110, low=106, close=108)
    after_no_touch = observe_lifecycle(zone, partial, no_touch_bar)
    assert after_no_touch.status is FvgLifecycleStatus.PARTIALLY_FILLED
    assert after_no_touch.fill_percentage == Decimal("0")
    assert after_no_touch.maximum_penetration == Decimal("0.6")

    second_touch_bar = candle(6, open_=107, high=110, low=103, close=108)
    second_touch = observe_lifecycle(zone, after_no_touch, second_touch_bar)
    assert second_touch.contact_count == 2
    assert second_touch.first_touch_at < second_touch.last_touch_at

    full_bar = candle(7, open_=103, high=110, low=99, close=104)
    full = observe_lifecycle(zone, second_touch, full_bar)
    assert full.status is FvgLifecycleStatus.FULLY_FILLED
    assert full.maximum_penetration == Decimal("1")


def test_observe_lifecycle_fail_closed_paths() -> None:
    zone = _zone()
    active = _active()
    bar = candle(3, open_=107, high=110, low=106, close=108)
    with pytest.raises(TypeError):
        observe_lifecycle(object(), active, bar)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        observe_lifecycle(zone, object(), bar)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="belong"):
        observe_lifecycle(zone, replace(active, zone_id="other"), bar)
    with pytest.raises(ValueError, match="scope"):
        observe_lifecycle(zone, active, replace(bar, symbol="ETHUSDT"))

    observed = observe_lifecycle(zone, active, bar)
    with pytest.raises(ValueError, match="regressed"):
        _observe_lifecycle_validated(
            zone,
            observed,
            bar_close=zone.created_at,
            duration_seconds=900,
            bar_low=Decimal("106"),
            bar_high=Decimal("110"),
        )
    with pytest.raises(ValueError, match="align"):
        _observe_lifecycle_validated(
            zone,
            active,
            bar_close=zone.created_at + timedelta(minutes=16),
            duration_seconds=900,
            bar_low=Decimal("106"),
            bar_high=Decimal("110"),
        )


def test_terminal_lifecycle_transitions_and_guards() -> None:
    zone = _zone()
    partial = observe_lifecycle(
        zone,
        _active(),
        candle(3, open_=107, high=110, low=102, close=108),
    )
    expired = terminal_lifecycle(
        partial,
        FvgLifecycleStatus.EXPIRED,
        partial.last_observed_at,
    )
    assert expired.status is FvgLifecycleStatus.EXPIRED
    assert terminal_lifecycle(expired, FvgLifecycleStatus.EXPIRED, expired.last_observed_at) is expired
    with pytest.raises(ValueError, match="rewritten"):
        terminal_lifecycle(expired, FvgLifecycleStatus.ARCHIVED, expired.last_observed_at)
    with pytest.raises(TypeError):
        terminal_lifecycle(object(), FvgLifecycleStatus.EXPIRED, partial.last_observed_at)  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        terminal_lifecycle(partial, "expired", partial.last_observed_at)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="terminal status"):
        terminal_lifecycle(partial, FvgLifecycleStatus.ACTIVE, partial.last_observed_at)
    with pytest.raises(ValueError, match="latest"):
        terminal_lifecycle(
            partial,
            FvgLifecycleStatus.EXPIRED,
            partial.last_observed_at + timedelta(minutes=15),
        )

    fully_filled = observe_lifecycle(
        zone,
        _active(),
        candle(3, open_=103, high=110, low=99, close=104),
    )
    terminal = terminal_lifecycle(
        fully_filled,
        FvgLifecycleStatus.FULLY_FILLED,
        fully_filled.last_observed_at,
    )
    assert terminal == fully_filled
