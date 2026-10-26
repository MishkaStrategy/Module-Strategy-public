"""Test-only in-memory CAS reference for complete allocation snapshots.

This is an executable model only. It provides no durable or cross-process
fencing and does not integrate with dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Callable

from paper_allocation_reference import AllocationState, KernelConflict, KernelStale


def validate_snapshot(state: AllocationState) -> None:
    """Reject malformed collections and duplicate identities at the authority edge."""
    collections = (
        state.reservations,
        state.cycle_counts,
        state.closed_cycles,
        state.decisions,
        state.fill_records,
        state.events,
    )
    if any(type(rows) is not tuple for rows in collections):
        raise KernelConflict("allocation snapshot collections must be tuples")
    identities = (
        ([row.request_id for row in state.reservations], "reservation request"),
        ([(row[0], row[1]) for row in state.cycle_counts], "cycle count"),
        ([row.cycle_id for row in state.closed_cycles], "closed cycle"),
        ([row.request_id for row in state.decisions], "decision request"),
        ([row.receipt.fill_id for row in state.fill_records], "fill"),
        ([row.event_id for row in state.events], "event"),
    )
    for values, label in identities:
        if len(values) != len(set(values)):
            raise KernelConflict(f"allocation snapshot contains duplicate {label} identity")
    if len(state.events) != state.revision:
        raise KernelConflict("event ledger length must equal allocation revision")
    for index, event in enumerate(state.events):
        if event.expected_revision != index or event.result_revision != index + 1:
            raise KernelConflict("event ledger does not form a contiguous revision chain")
        if event.scope != state.scope:
            raise KernelConflict("event ledger contains a different allocation scope")


@dataclass(slots=True)
class InMemoryAllocationAuthority:
    """Single-process compare-and-swap over the entire immutable snapshot."""

    _current: AllocationState
    _lock: Lock = field(default_factory=Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        validate_snapshot(self._current)

    @property
    def current(self) -> AllocationState:
        return self._current

    def compare_and_swap(
        self,
        *,
        expected: AllocationState,
        expected_scope_id: str,
        expected_generation: str,
        expected_policy_id: str,
        candidate: AllocationState,
        before_publish: Callable[[], None] | None = None,
    ) -> AllocationState:
        """Validate and atomically replace the full state, retaining CAS on failure."""
        with self._lock:
            validate_snapshot(expected)
            validate_snapshot(candidate)
            current = self._current
            if current != expected or current.revision != expected.revision:
                raise KernelStale("authoritative allocation snapshot has advanced")
            if (
                current.scope.scope_id != expected_scope_id
                or current.scope.generation != expected_generation
                or current.policy_id != expected_policy_id
            ):
                raise KernelConflict("expected allocation identity does not match authority")
            if (
                candidate.scope != current.scope
                or candidate.policy_id != current.policy_id
                or candidate.policy_fingerprint != current.policy_fingerprint
            ):
                raise KernelConflict("candidate allocation identity does not match authority")
            if candidate.revision != current.revision + 1:
                raise KernelConflict("candidate revision must advance exactly once")
            if candidate.events[: len(current.events)] != current.events:
                raise KernelConflict("candidate event ledger does not retain exact prior prefix")
            if len(candidate.events) != len(current.events) + 1:
                raise KernelConflict("candidate must append exactly one event")
            appended = candidate.events[-1]
            if (
                appended.expected_revision != current.revision
                or appended.result_revision != candidate.revision
                or appended.scope != current.scope
            ):
                raise KernelConflict("candidate event does not describe the revision transition")
            # All checks and the optional pre-publication step complete before mutation.
            if before_publish is not None:
                before_publish()
            self._current = candidate
            return candidate

    def allocation_read(self) -> AllocationState:
        return self._readable_snapshot()

    def dispatch_read(self) -> AllocationState:
        return self._readable_snapshot()

    def _readable_snapshot(self) -> AllocationState:
        with self._lock:
            snapshot = self._current
            validate_snapshot(snapshot)
            if snapshot.recovering:
                raise KernelConflict("recovering allocation aggregate is unavailable for reads")
            return snapshot
