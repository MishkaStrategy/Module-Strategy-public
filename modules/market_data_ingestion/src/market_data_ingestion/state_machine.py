from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager, suppress
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime

from .enums import RunState
from .errors import ContractError

TransitionObserver = Callable[[RunState, RunState, datetime], None]
_TRANSITION_OBSERVER: ContextVar[TransitionObserver | None] = ContextVar(
    "market_data_transition_observer",
    default=None,
)
_TRANSITION_OBSERVER_ACTIVE: ContextVar[bool] = ContextVar(
    "market_data_transition_observer_active",
    default=False,
)

_TERMINAL = {
    RunState.COMPLETED,
    RunState.COMPLETED_WITH_WARNINGS,
    RunState.CANCELLED,
    RunState.FAILED,
    RunState.DATASET_REJECTED,
}

_ALLOWED: dict[RunState, frozenset[RunState]] = {
    RunState.CREATED: frozenset({RunState.VALIDATING_REQUEST, RunState.CANCELLED}),
    RunState.VALIDATING_REQUEST: frozenset(
        {
            RunState.DISCOVERING_CAPABILITIES,
            RunState.EXPORTING,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.DISCOVERING_CAPABILITIES: frozenset(
        {
            RunState.RESOLVING_INSTRUMENT,
            RunState.FAILED,
            RunState.PAUSED,
            RunState.CANCELLED,
        }
    ),
    RunState.RESOLVING_INSTRUMENT: frozenset(
        {RunState.PLANNING, RunState.FAILED, RunState.CANCELLED}
    ),
    RunState.PLANNING: frozenset(
        {
            RunState.DOWNLOADING,
            RunState.REPAIRING,
            RunState.FAILED,
            RunState.PAUSED,
            RunState.CANCELLED,
        }
    ),
    RunState.DOWNLOADING: frozenset(
        {
            RunState.VERIFYING_RAW,
            RunState.PAUSED,
            RunState.FAILED,
            RunState.CANCELLED,
        }
    ),
    RunState.VERIFYING_RAW: frozenset(
        {
            RunState.NORMALIZING,
            RunState.REPAIRING,
            RunState.FAILED,
            RunState.DATASET_REJECTED,
        }
    ),
    RunState.NORMALIZING: frozenset({RunState.VALIDATING_DATA, RunState.FAILED, RunState.PAUSED}),
    RunState.VALIDATING_DATA: frozenset(
        {
            RunState.REPAIRING,
            RunState.BUILDING_DERIVED,
            RunState.HASHING,
            RunState.DATASET_REJECTED,
            RunState.FAILED,
        }
    ),
    RunState.REPAIRING: frozenset(
        {
            RunState.VALIDATING_DATA,
            RunState.DATASET_REJECTED,
            RunState.PAUSED,
            RunState.FAILED,
        }
    ),
    RunState.BUILDING_DERIVED: frozenset({RunState.HASHING, RunState.FAILED, RunState.PAUSED}),
    RunState.HASHING: frozenset({RunState.REGISTERING, RunState.FAILED}),
    RunState.REGISTERING: frozenset(
        {
            RunState.COMPLETED,
            RunState.COMPLETED_WITH_WARNINGS,
            RunState.PAUSED,
            RunState.FAILED,
        }
    ),
    RunState.EXPORTING: frozenset({RunState.COMPLETED, RunState.FAILED}),
    RunState.PAUSED: frozenset(
        {
            RunState.DISCOVERING_CAPABILITIES,
            RunState.PLANNING,
            RunState.DOWNLOADING,
            RunState.NORMALIZING,
            RunState.REPAIRING,
            RunState.BUILDING_DERIVED,
            RunState.REGISTERING,
            RunState.CANCELLED,
            RunState.FAILED,
        }
    ),
    RunState.COMPLETED: frozenset(),
    RunState.COMPLETED_WITH_WARNINGS: frozenset(),
    RunState.CANCELLED: frozenset(),
    RunState.FAILED: frozenset(),
    RunState.DATASET_REJECTED: frozenset(),
}


@contextmanager
def observe_transitions(observer: TransitionObserver) -> Iterator[None]:
    """Observe committed state transitions without giving observers control of execution.

    Observer failures are deliberately isolated from the ingestion state machine. The
    callback is scoped with a ContextVar so concurrent application jobs can attach their
    own sinks without mutating global process state. Nested transitions made from inside
    an observer are not re-emitted to the same observer, preventing recursive side
    effects and lock re-entry.
    """

    if not callable(observer):
        raise TypeError("observer must be callable")
    token = _TRANSITION_OBSERVER.set(observer)
    try:
        yield
    finally:
        _TRANSITION_OBSERVER.reset(token)


@dataclass(slots=True)
class IngestionStateMachine:
    state: RunState = RunState.CREATED
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def transition(self, target: RunState, *, at: datetime | None = None) -> None:
        if target not in _ALLOWED[self.state]:
            raise ContractError(
                f"invalid ingestion transition {self.state.value} -> {target.value}",
                details={"state": self.state.value, "target": target.value},
            )
        previous = self.state
        self.state = target
        self.updated_at = at or datetime.now(UTC)
        observer = _TRANSITION_OBSERVER.get()
        if observer is None or _TRANSITION_OBSERVER_ACTIVE.get():
            return
        active_token = _TRANSITION_OBSERVER_ACTIVE.set(True)
        try:
            # Observability is non-authoritative and must never alter domain outcomes.
            with suppress(Exception):
                observer(previous, target, self.updated_at)
        finally:
            _TRANSITION_OBSERVER_ACTIVE.reset(active_token)

    @property
    def terminal(self) -> bool:
        return self.state in _TERMINAL
