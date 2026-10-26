from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from modular_strategy_position_exit_management import PositionExitManagementModule


def test_restore_rejects_position_entry_after_saved_event_cursor(
    view_factory,
    candle_factory,
    position_factory,
):
    source = PositionExitManagementModule()
    source.on_bar(
        view_factory(
            0,
            candle=candle_factory(0),
            position=position_factory(),
        )
    )
    valid = source.snapshot_state()
    broken = deepcopy(valid)
    cursor = candle_factory(0).close_time
    broken["active_position"]["entry_time"] = (cursor + timedelta(seconds=1)).isoformat()

    target = PositionExitManagementModule()
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="entry_time cannot exceed event cursor"):
        target.restore_state(broken)
    assert target.snapshot_state() == before
