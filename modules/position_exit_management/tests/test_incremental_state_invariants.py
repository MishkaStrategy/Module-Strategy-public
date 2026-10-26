from __future__ import annotations

from copy import deepcopy

import pytest

from modular_strategy_position_exit_management import PositionExitManagementModule


def _checkpoint(module, view_factory, candle_factory, position_factory):
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=100, high=101, low=99, close=100),
            position=position_factory(),
        )
    )
    return module.snapshot_state()


def _assert_atomic_rejection(module, original, broken, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        module.restore_state(broken)
    assert module.snapshot_state() == original


def test_restore_rejects_noncanonical_utc_timestamp_atomically(
    view_factory, candle_factory, position_factory
) -> None:
    module = PositionExitManagementModule()
    original = _checkpoint(module, view_factory, candle_factory, position_factory)
    broken = deepcopy(original)
    broken["last_close_time"] = broken["last_close_time"].replace("+00:00", "Z")
    _assert_atomic_rejection(module, original, broken, "canonical UTC ISO-8601")


def test_restore_rejects_initial_stop_without_positive_risk_atomically(
    view_factory, candle_factory, position_factory
) -> None:
    module = PositionExitManagementModule()
    original = _checkpoint(module, view_factory, candle_factory, position_factory)
    broken = deepcopy(original)
    broken["active_position"]["initial_stop"] = broken["active_position"]["entry_price"]
    broken["active_position"]["initial_risk_per_unit"] = None
    _assert_atomic_rejection(module, original, broken, "positive initial risk")
