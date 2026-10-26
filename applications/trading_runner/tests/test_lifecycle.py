from __future__ import annotations

import pytest

from modular_trading_runner import LifecycleMachine, LifecycleTransitionError, RuntimeState


def test_lifecycle_rejects_skipping_recovery_and_reconciliation() -> None:
    machine = LifecycleMachine()
    machine.transition(RuntimeState.CONFIGURING)
    machine.transition(RuntimeState.CONFIGURED)
    machine.transition(RuntimeState.INITIALIZING)
    with pytest.raises(LifecycleTransitionError):
        machine.transition(RuntimeState.RUNNING)


def test_stopped_state_cannot_return_to_running() -> None:
    machine = LifecycleMachine(RuntimeState.STOPPED)
    with pytest.raises(LifecycleTransitionError):
        machine.transition(RuntimeState.RUNNING)


def test_same_state_transition_is_idempotent() -> None:
    machine = LifecycleMachine(RuntimeState.RUNNING)
    machine.transition(RuntimeState.RUNNING)
    assert machine.state is RuntimeState.RUNNING
