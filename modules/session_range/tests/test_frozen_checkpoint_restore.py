from __future__ import annotations

import pytest
from modular_core import capture_module_states, restore_module_states

from modular_session_range import SessionRangeModule


def make_module() -> SessionRangeModule:
    return SessionRangeModule(
        [
            {
                "name": "x",
                "timezone": "UTC",
                "start": "00:00",
                "end": "00:15",
            }
        ]
    )


def test_core_frozen_checkpoint_payload_restores() -> None:
    source = make_module()
    states = capture_module_states([source])

    assert len(states) == 1
    assert isinstance(states[0].payload["active"], tuple)

    restored = make_module()
    restore_module_states([restored], states)

    assert restored.snapshot_state() == source.snapshot_state()


@pytest.mark.parametrize("invalid", ({}, "active", {"not-a-sequence"}, 1, None))
def test_restore_active_still_rejects_non_sequence_containers(invalid: object) -> None:
    state = dict(make_module().snapshot_state())
    state["active"] = invalid

    with pytest.raises(ValueError, match="state.active"):
        make_module().restore_state(state)
