from __future__ import annotations

from dataclasses import dataclass

from .enums import RuntimeState
from .errors import LifecycleTransitionError

_ALLOWED: dict[RuntimeState, frozenset[RuntimeState]] = {
    RuntimeState.CREATED: frozenset({RuntimeState.CONFIGURING, RuntimeState.DISPOSED}),
    RuntimeState.CONFIGURING: frozenset({RuntimeState.CONFIGURED, RuntimeState.FAILED}),
    RuntimeState.CONFIGURED: frozenset(
        {RuntimeState.INITIALIZING, RuntimeState.STOPPING, RuntimeState.FAILED}
    ),
    RuntimeState.INITIALIZING: frozenset({RuntimeState.RECOVERING, RuntimeState.FAILED}),
    RuntimeState.RECOVERING: frozenset({RuntimeState.RECONCILING, RuntimeState.FAILED}),
    RuntimeState.RECONCILING: frozenset({RuntimeState.STARTING, RuntimeState.FAILED}),
    RuntimeState.STARTING: frozenset(
        {RuntimeState.RUNNING, RuntimeState.DEGRADED, RuntimeState.FAILED}
    ),
    RuntimeState.RUNNING: frozenset(
        {
            RuntimeState.DEGRADED,
            RuntimeState.PAUSING,
            RuntimeState.KILL_SWITCH_ACTIVE,
            RuntimeState.STOPPING,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.DEGRADED: frozenset(
        {
            RuntimeState.RUNNING,
            RuntimeState.KILL_SWITCH_ACTIVE,
            RuntimeState.STOPPING,
            RuntimeState.FAILED,
        }
    ),
    RuntimeState.PAUSING: frozenset(
        {RuntimeState.PAUSED, RuntimeState.STOPPING, RuntimeState.FAILED}
    ),
    RuntimeState.PAUSED: frozenset(
        {RuntimeState.STARTING, RuntimeState.STOPPING, RuntimeState.FAILED}
    ),
    RuntimeState.KILL_SWITCH_ACTIVE: frozenset(
        {RuntimeState.RUNNING, RuntimeState.DEGRADED, RuntimeState.STOPPING, RuntimeState.FAILED}
    ),
    RuntimeState.STOPPING: frozenset({RuntimeState.STOPPED, RuntimeState.FAILED}),
    RuntimeState.STOPPED: frozenset({RuntimeState.DISPOSED}),
    RuntimeState.FAILED: frozenset({RuntimeState.STOPPING, RuntimeState.DISPOSED}),
    RuntimeState.DISPOSED: frozenset(),
}


@dataclass(slots=True)
class LifecycleMachine:
    state: RuntimeState = RuntimeState.CREATED

    def transition(self, target: RuntimeState) -> None:
        if not isinstance(target, RuntimeState):
            raise TypeError("target must be RuntimeState")
        if target is self.state:
            return
        if target not in _ALLOWED[self.state]:
            raise LifecycleTransitionError(
                f"forbidden lifecycle transition: {self.state.value} -> {target.value}"
            )
        self.state = target

    def force_failed(self) -> None:
        if self.state in {RuntimeState.STOPPED, RuntimeState.DISPOSED}:
            raise LifecycleTransitionError(f"terminal state {self.state.value} cannot fail")
        self.state = RuntimeState.FAILED
