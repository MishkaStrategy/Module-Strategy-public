from __future__ import annotations

from copy import deepcopy

import pytest

from modular_strategy_position_exit_management import (
    STATE_SCHEMA_VERSION,
    PositionExitManagementModule,
    PositionExitReason,
)


def _populated_state(module, view_factory, candle_factory, position_factory):
    module.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(),
        )
    )
    return dict(module.snapshot_state())


def test_snapshot_is_json_compatible_and_versioned(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    state = _populated_state(module, view_factory, candle_factory, position_factory)
    assert state["schema_version"] == STATE_SCHEMA_VERSION
    assert isinstance(state["config_fingerprint"], str)
    assert state["active_position"]["entry_price"] == "100"
    assert state["active_position"]["initial_risk_per_unit"] == "5"


def test_restore_round_trip_is_deterministic(
    view_factory,
    candle_factory,
    position_factory,
):
    original = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    state = _populated_state(original, view_factory, candle_factory, position_factory)
    restored = PositionExitManagementModule(
        partial_exits=[{"trigger_r": 1, "fraction": 0.5}],
        breakeven_trigger_r=1,
    )
    restored.restore_state(state)
    assert restored.snapshot_state() == original.snapshot_state()

    duplicate = restored.on_bar(
        view_factory(
            0,
            candle=candle_factory(0, open_=104, high=106, low=103, close=105),
            position=position_factory(),
        )
    )
    assert duplicate.skips == (PositionExitReason.DUPLICATE_EVENT.value,)


def test_empty_restore_resets_module(view_factory, candle_factory, position_factory):
    module = PositionExitManagementModule()
    _populated_state(module, view_factory, candle_factory, position_factory)
    module.restore_state({})
    assert module.snapshot_state()["active_position"] is None
    assert module.snapshot_state()["last_close_time"] is None


@pytest.mark.parametrize(
    "mutation",
    [
        lambda state: state.update(schema_version="999"),
        lambda state: state.update(config_fingerprint="wrong"),
        lambda state: state.update(unknown=True),
        lambda state: state.pop("stream_symbol"),
        lambda state: state.update(last_close_time="not-a-time"),
        lambda state: state.update(last_event_fingerprint=" "),
        lambda state: state.update(cancel_attempts=[]),
        lambda state: state.update(stream_symbol="BTCUSDT", stream_timeframe=None),
    ],
)
def test_corrupt_top_level_state_is_rejected_atomically(
    view_factory,
    candle_factory,
    position_factory,
    mutation,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    valid = _populated_state(module, view_factory, candle_factory, position_factory)
    before = deepcopy(valid)
    broken = deepcopy(valid)
    mutation(broken)
    with pytest.raises(ValueError):
        module.restore_state(broken)
    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    "field,value",
    [
        ("position_id", " "),
        ("side", "flat"),
        ("entry_price", "100.0"),
        ("entry_price", "NaN"),
        ("entry_time", "2026-01-01T00:00:00"),
        ("initial_quantity", "0"),
        ("initial_stop", "0"),
        ("initial_risk_per_unit", "4"),
        ("favorable_extreme", "90"),
        ("last_seen_quantity", "-1"),
        ("full_close_requested", 1),
        ("bars_managed", -1),
    ],
)
def test_corrupt_position_state_is_rejected(
    view_factory,
    candle_factory,
    position_factory,
    field,
    value,
):
    module = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    state = _populated_state(module, view_factory, candle_factory, position_factory)
    broken = deepcopy(state)
    broken["active_position"][field] = value
    with pytest.raises((ValueError, TypeError)):
        module.restore_state(broken)


def test_partial_indices_must_match_current_config(
    view_factory,
    candle_factory,
    position_factory,
):
    source = PositionExitManagementModule(partial_exits=[{"trigger_r": 1, "fraction": 0.5}])
    state = _populated_state(source, view_factory, candle_factory, position_factory)
    broken = deepcopy(state)
    broken["active_position"]["pending_partial_index"] = 4
    with pytest.raises(ValueError):
        source.restore_state(broken)


def test_restore_rejects_oversized_cancel_tracking(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule(
        max_tracked_cancels=2,
        max_cancel_requests_per_bar=2,
    )
    state = _populated_state(module, view_factory, candle_factory, position_factory)
    broken = deepcopy(state)
    broken["cancel_attempts"] = {"a": 1, "b": 2, "c": 3}
    with pytest.raises(ValueError):
        module.restore_state(broken)


def test_config_fingerprint_blocks_incompatible_restore(
    view_factory,
    candle_factory,
    position_factory,
):
    source = PositionExitManagementModule(breakeven_trigger_r=1)
    state = _populated_state(source, view_factory, candle_factory, position_factory)
    incompatible = PositionExitManagementModule(breakeven_trigger_r=2)
    with pytest.raises(ValueError, match="fingerprint"):
        incompatible.restore_state(state)


def test_two_instances_have_independent_state(
    view_factory,
    candle_factory,
    position_factory,
):
    first = PositionExitManagementModule(max_holding_minutes=5)
    second = PositionExitManagementModule(max_holding_minutes=5)
    view = view_factory(0, candle=candle_factory(0), position=position_factory())
    first.on_bar(view)
    assert first.snapshot_state()["active_position"] is not None
    assert second.snapshot_state()["active_position"] is None


def test_snapshot_mutation_does_not_mutate_module(
    view_factory,
    candle_factory,
    position_factory,
):
    module = PositionExitManagementModule()
    state = _populated_state(module, view_factory, candle_factory, position_factory)
    state["active_position"]["position_id"] = "mutated"
    assert module.snapshot_state()["active_position"]["position_id"] == "position-1"
