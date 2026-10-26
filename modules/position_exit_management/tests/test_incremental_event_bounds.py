from __future__ import annotations

from copy import deepcopy
from dataclasses import replace

import pytest

from modular_strategy_position_exit_management import (
    PositionExitConfig,
    PositionExitManagementModule,
)

MAX_COUNTER_VALUE = 2**63 - 1


def test_empty_market_view_fails_before_state_mutation(view_factory):
    module = PositionExitManagementModule()
    view = replace(view_factory(), base_bars=())
    before = module.snapshot_state()

    with pytest.raises(ValueError, match="base_bars cannot be empty"):
        module.on_bar(view)

    assert module.snapshot_state() == before


def test_non_sequence_market_bars_fail_before_state_mutation(view_factory):
    module = PositionExitManagementModule()
    view = replace(view_factory(), base_bars="not-candles")
    before = module.snapshot_state()

    with pytest.raises(TypeError, match="base_bars must be a candle sequence"):
        module.on_bar(view)

    assert module.snapshot_state() == before


def test_non_candle_current_value_fails_before_state_mutation(view_factory):
    module = PositionExitManagementModule()
    view = replace(view_factory(), base_bars=("not-a-candle",))
    before = module.snapshot_state()

    with pytest.raises(TypeError, match="view.current must be Candle"):
        module.on_bar(view)

    assert module.snapshot_state() == before


def test_unrepresentable_current_close_time_fails_before_state_mutation(view_factory):
    module = PositionExitManagementModule()
    source = view_factory()
    malformed = replace(
        source.current,
        timeframe="999999999999999999999999999999d",
    )
    view = replace(source, base_bars=(malformed,))
    before = module.snapshot_state()

    with pytest.raises(ValueError, match="current.close_time must be representable"):
        module.on_bar(view)

    assert module.snapshot_state() == before


def test_unbounded_event_index_fails_before_stream_binding(view_factory):
    module = PositionExitManagementModule()
    view = replace(view_factory(), index=MAX_COUNTER_VALUE + 1)
    before = module.snapshot_state()

    with pytest.raises(ValueError, match="view.index cannot exceed"):
        module.on_bar(view)

    assert module.snapshot_state() == before


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("cancel_retry_bars", MAX_COUNTER_VALUE + 1),
        ("max_tracked_cancels", MAX_COUNTER_VALUE + 1),
        ("max_cancel_requests_per_bar", MAX_COUNTER_VALUE + 1),
    ],
)
def test_configuration_counters_use_bounded_integer_domain(field, value):
    with pytest.raises(ValueError, match=f"{field} cannot exceed"):
        PositionExitManagementModule(**{field: value})


def test_direct_config_rejects_unconverted_enum_values():
    with pytest.raises(TypeError, match="trailing_mode must be TrailingMode"):
        PositionExitConfig(trailing_mode="none")  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="trigger_source must be TriggerPriceSource"):
        PositionExitConfig(trigger_source="close")  # type: ignore[arg-type]


def test_restore_rejects_unbounded_bars_managed_atomically(
    view_factory,
    position_factory,
):
    source = PositionExitManagementModule()
    source.on_bar(view_factory(position=position_factory()))
    broken = deepcopy(source.snapshot_state())
    broken["active_position"]["bars_managed"] = MAX_COUNTER_VALUE + 1

    target = PositionExitManagementModule()
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="bars_managed cannot exceed"):
        target.restore_state(broken)
    assert target.snapshot_state() == before


def test_restore_rejects_unbounded_cancel_attempt_index_atomically(
    view_factory,
    position_factory,
):
    source = PositionExitManagementModule()
    source.on_bar(view_factory(position=position_factory()))
    broken = deepcopy(source.snapshot_state())
    broken["cancel_attempts"] = {"order-1": MAX_COUNTER_VALUE + 1}

    target = PositionExitManagementModule()
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="cancel attempt index"):
        target.restore_state(broken)
    assert target.snapshot_state() == before
