from __future__ import annotations

from dataclasses import replace
from threading import Event, Thread, get_ident
from typing import cast

import pytest

import paper_allocation_commit_reference as allocation_commit_reference
from paper_allocation_commit_reference import InMemoryAllocationAuthority
from paper_allocation_reference import (
    AllocationPolicy,
    AllocationState,
    EventRecord,
    FillReceipt,
    KernelConflict,
    KernelStale,
    allocate_cycle,
    apply_fill,
    initial_state,
)
from test_paper_allocation_reference import MEMBERS, SCOPE, fixture_policy, request


def branches() -> tuple[AllocationPolicy, AllocationState, AllocationState, AllocationState]:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    base = initial_state(policy, SCOPE)
    first = allocate_cycle(
        base,
        policy,
        (request("branch-one", MEMBERS[0]),),
        event_id="branch-one-event",
        cycle_id="cycle-1",
        expected_scope=SCOPE,
        expected_revision=base.revision,
    )
    second = allocate_cycle(
        base,
        policy,
        (request("branch-two", MEMBERS[1]),),
        event_id="branch-two-event",
        cycle_id="cycle-1",
        expected_scope=SCOPE,
        expected_revision=base.revision,
    )
    return policy, base, first, second


def publish(
    authority: InMemoryAllocationAuthority,
    base: AllocationState,
    candidate: AllocationState,
) -> AllocationState:
    return authority.compare_and_swap(
        expected=base,
        expected_scope_id=SCOPE.scope_id,
        expected_generation=SCOPE.generation,
        expected_policy_id="test-only-paper-allocation-v1",
        candidate=candidate,
    )


def test_competing_candidates_from_same_snapshot_only_one_wins_whole_state() -> None:
    _, base, first, second = branches()
    authority = InMemoryAllocationAuthority(base)

    assert publish(authority, base, first) == first
    with pytest.raises(KernelStale, match="advanced"):
        publish(authority, base, second)
    assert authority.current is first
    assert authority.current.decisions == first.decisions
    assert authority.current.reservations == first.reservations
    assert authority.current.events == first.events
    assert second not in (authority.current,)


def test_prepublication_failure_leaves_authoritative_snapshot_unchanged() -> None:
    _, base, candidate, _ = branches()
    authority = InMemoryAllocationAuthority(base)

    def fail() -> None:
        raise RuntimeError("synthetic publication preparation failure")

    with pytest.raises(RuntimeError, match="synthetic"):
        authority.compare_and_swap(
            expected=base,
            expected_scope_id=SCOPE.scope_id,
            expected_generation=SCOPE.generation,
            expected_policy_id=base.policy_id,
            candidate=candidate,
            before_publish=fail,
        )
    assert authority.current is base


def test_recovering_snapshot_is_unavailable_to_allocation_and_dispatch_reads() -> None:
    policy, _, allocated, _ = branches()
    recovering = apply_fill(
        allocated,
        policy,
        FillReceipt("uncertain-fill", "branch-one", MEMBERS[0], SCOPE, 999, 999),
        event_id="uncertain-fill-event",
        expected_scope=SCOPE,
        expected_revision=allocated.revision,
    )
    authority = InMemoryAllocationAuthority(recovering)

    for read in (authority.allocation_read, authority.dispatch_read):
        with pytest.raises(KernelConflict, match="recovering"):
            read()


@pytest.mark.parametrize("read_name", ["allocation_read", "dispatch_read"])
def test_read_returns_the_snapshot_it_validated_while_cas_waits(
    read_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy, _, healthy, _ = branches()
    recovering = apply_fill(
        healthy,
        policy,
        FillReceipt("uncertain-fill", "branch-one", MEMBERS[0], SCOPE, 999, 999),
        event_id="uncertain-fill-event",
        expected_scope=SCOPE,
        expected_revision=healthy.revision,
    )
    authority = InMemoryAllocationAuthority(healthy)
    validation_started = Event()
    allow_validation = Event()
    writer_started = Event()
    reader_identity: int | None = None
    read_results: list[AllocationState] = []
    read_errors: list[BaseException] = []
    write_errors: list[BaseException] = []
    original_validate = allocation_commit_reference.validate_snapshot

    def controlled_validate(state: AllocationState) -> None:
        if get_ident() == reader_identity and state is healthy:
            validation_started.set()
            if not allow_validation.wait(timeout=2):
                raise AssertionError("test did not release the controlled snapshot validation")
        original_validate(state)

    monkeypatch.setattr(allocation_commit_reference, "validate_snapshot", controlled_validate)

    def read() -> None:
        nonlocal reader_identity
        reader_identity = get_ident()
        try:
            read_results.append(getattr(authority, read_name)())
        except BaseException as exc:
            read_errors.append(exc)

    def write() -> None:
        writer_started.set()
        try:
            publish(authority, healthy, recovering)
        except BaseException as exc:
            write_errors.append(exc)

    reader = Thread(target=read, name="allocation-snapshot-reader")
    reader.start()
    assert validation_started.wait(timeout=2)
    writer = Thread(target=write, name="allocation-snapshot-writer")
    writer.start()
    assert writer_started.wait(timeout=2)
    allow_validation.set()
    reader.join(timeout=2)
    writer.join(timeout=2)

    assert not reader.is_alive()
    assert not writer.is_alive()
    assert read_errors == []
    assert write_errors == []
    assert read_results == [healthy]
    assert authority.current is recovering
    with pytest.raises(KernelConflict, match="recovering"):
        getattr(authority, read_name)()


def test_compare_and_swap_checks_identity_revision_and_event_prefix() -> None:
    _, base, candidate, _ = branches()
    authority = InMemoryAllocationAuthority(base)

    with pytest.raises(KernelConflict, match="identity"):
        authority.compare_and_swap(
            expected=base,
            expected_scope_id=SCOPE.scope_id,
            expected_generation="other-generation",
            expected_policy_id=base.policy_id,
            candidate=candidate,
        )
    with pytest.raises(KernelConflict, match="revision"):
        authority.compare_and_swap(
            expected=base,
            expected_scope_id=SCOPE.scope_id,
            expected_generation=SCOPE.generation,
            expected_policy_id=base.policy_id,
            candidate=replace(candidate, revision=2),
        )
    assert authority.current is base


def test_authority_rejects_non_tuple_snapshot_collections() -> None:
    _, base, _, _ = branches()

    with pytest.raises(KernelConflict, match="must be tuples"):
        InMemoryAllocationAuthority(replace(base, events=cast(tuple[EventRecord, ...], [])))


def test_authority_rejects_malformed_event_revision_chain() -> None:
    _, _, candidate, _ = branches()
    malformed = replace(
        candidate,
        events=(replace(candidate.events[0], expected_revision=7),),
    )

    with pytest.raises(KernelConflict, match="contiguous revision chain"):
        InMemoryAllocationAuthority(malformed)
