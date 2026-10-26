from __future__ import annotations

from dataclasses import FrozenInstanceError, replace

import pytest

from paper_allocation_reference import (
    AllocationPolicy,
    AllocationRequest,
    AllocationScope,
    AllocationState,
    FillReceipt,
    KernelConflict,
    KernelError,
    KernelStale,
    KernelUnconfigured,
    TerminalReceipt,
    allocate_cycle,
    apply_fill,
    apply_terminal,
    initial_state,
    observe_cancel_or_lost_ack,
)


# Every value below is synthetic test policy; none is a production default or
# an approved financial formula, authority, limit, or cycle rule.
SCOPE = AllocationScope(
    scope_id="fixture-composition-scope",
    generation="fixture-generation-1",
    unit_id="fixture-credit-unit",
    scale=100,
)
MEMBERS = ("strategy-alpha", "strategy-beta", "strategy-gamma")


def fixture_policy(
    *,
    opening_budget: int = 1_000,
    continue_after_insufficient_funds: bool,
    eligible_cycle_ids: tuple[str, ...] = ("cycle-1", "cycle-2", "cycle-3"),
) -> AllocationPolicy:
    return AllocationPolicy(
        policy_id="test-only-paper-allocation-v1",
        scope=SCOPE,
        opening_budget=opening_budget,
        max_commitment_units=900,
        max_cost_units=200,
        priority_direction="ascending",
        priority_min=0,
        priority_max=9,
        membership=MEMBERS,
        eligible_cycle_ids=eligible_cycle_ids,
        eligible_members=MEMBERS[:2],
        max_requests_per_strategy_per_cycle=1,
        continue_after_insufficient_funds=continue_after_insufficient_funds,
        capital_authority_id="fixture-capital-authority",
        commitment_authority_id="fixture-commitment-authority",
        cost_authority_id="fixture-cost-authority",
        rank_authority_id="fixture-rank-authority",
        terminal_authority_id="fixture-terminal-authority",
        reconciliation_authority_id="fixture-reconciliation-authority",
    )


def request(
    request_id: str,
    strategy: str,
    *,
    priority: int = 1,
    commitment: int = 300,
    cost: int = 50,
    cycle: str = "cycle-1",
    eligible: bool = True,
    scope: AllocationScope = SCOPE,
) -> AllocationRequest:
    return AllocationRequest(
        request_id=request_id,
        strategy_instance_id=strategy,
        cycle_id=cycle,
        scope=scope,
        priority=priority,
        commitment_units=commitment,
        cost_bound_units=cost,
        eligible=eligible,
    )


def allocate(
    state: AllocationState,
    policy: AllocationPolicy,
    rows: tuple[AllocationRequest, ...],
    *,
    event: str = "allocate-event",
    cycle: str = "cycle-1",
) -> AllocationState:
    return allocate_cycle(
        state,
        policy,
        rows,
        event_id=event,
        cycle_id=cycle,
        expected_scope=state.scope,
        expected_revision=state.revision,
    )


def fill(
    state: AllocationState,
    policy: AllocationPolicy,
    *,
    event: str,
    fill_id: str,
    request_id: str = "request-alpha",
    strategy: str = MEMBERS[0],
    commitment: int = 100,
    cost: int = 10,
    scope: AllocationScope = SCOPE,
) -> AllocationState:
    receipt = FillReceipt(fill_id, request_id, strategy, scope, commitment, cost)
    return apply_fill(
        state,
        policy,
        receipt,
        event_id=event,
        expected_scope=state.scope,
        expected_revision=state.revision,
    )


def test_fixture_policy_is_explicit_nonzero_scaled_and_immutable() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)

    assert policy.scope == SCOPE
    assert policy.opening_budget == 1_000
    assert policy.scope.unit_id == "fixture-credit-unit"
    assert policy.scope.scale == 100
    assert policy.max_commitment_units == 900
    assert policy.max_cost_units == 200
    assert policy.priority_direction == "ascending"
    assert (policy.priority_min, policy.priority_max) == (0, 9)
    assert policy.membership == MEMBERS
    assert policy.eligible_cycle_ids == ("cycle-1", "cycle-2", "cycle-3")
    assert policy.eligible_members == MEMBERS[:2]
    assert policy.max_requests_per_strategy_per_cycle == 1
    assert policy.continue_after_insufficient_funds is True
    assert (policy.capital_authority_id or "").startswith("fixture-")
    assert (state.available, state.reserved, state.exposure, state.consumed_costs) == (
        1_000,
        0,
        0,
        0,
    )
    with pytest.raises(FrozenInstanceError):
        policy.opening_budget = 0  # type: ignore[misc]


@pytest.mark.parametrize("field", ["scope_id", "generation", "unit_id"])
def test_scope_rejects_whitespace_identity_fields(field: str) -> None:
    with pytest.raises(KernelConflict, match=field):
        replace(SCOPE, **{field: "   "})


@pytest.mark.parametrize(
    "field",
    [
        "policy_id",
        "capital_authority_id",
        "commitment_authority_id",
        "cost_authority_id",
        "rank_authority_id",
        "terminal_authority_id",
        "reconciliation_authority_id",
    ],
)
def test_policy_and_authority_ids_reject_whitespace_before_initialization(field: str) -> None:
    policy = replace(fixture_policy(continue_after_insufficient_funds=True), **{field: "   "})

    with pytest.raises(KernelConflict, match=field):
        initial_state(policy, SCOPE)


@pytest.mark.parametrize("field", ["membership", "eligible_cycle_ids", "eligible_members"])
def test_policy_identity_sequences_reject_whitespace_before_initialization(field: str) -> None:
    policy = replace(fixture_policy(continue_after_insufficient_funds=True), **{field: ("   ",)})

    with pytest.raises(KernelConflict, match=field):
        initial_state(policy, SCOPE)


def test_unconfigured_policy_fails_closed_without_state() -> None:
    policy = replace(
        fixture_policy(continue_after_insufficient_funds=True), capital_authority_id=None
    )

    with pytest.raises(KernelUnconfigured, match="capital_authority_id"):
        initial_state(policy, SCOPE)


def test_whitespace_event_id_is_rejected_without_state_mutation() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)

    with pytest.raises(KernelConflict, match="event_id.*non-empty"):
        allocate(state, policy, (request("request-alpha", MEMBERS[0]),), event="   ")

    assert state.revision == 0
    assert state.available == state.initial_budget
    assert not state.events
    assert not state.closed_cycles


@pytest.mark.parametrize("field", ["strategy_instance_id", "cycle_id"])
def test_request_identity_fields_reject_whitespace_without_state_mutation(field: str) -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)
    row = replace(request("request-alpha", MEMBERS[0]), **{field: "   "})

    with pytest.raises(KernelConflict, match=field):
        allocate(state, policy, (row,))

    assert state.revision == 0
    assert state.available == state.initial_budget
    assert not state.events
    assert not state.closed_cycles


def test_allocation_cycle_argument_rejects_whitespace_without_state_mutation() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)

    with pytest.raises(KernelConflict, match="cycle_id"):
        allocate(state, policy, (request("request-alpha", MEMBERS[0]),), cycle="   ")

    assert state.revision == 0
    assert state.available == state.initial_budget
    assert not state.events


def test_mutable_policy_sequence_fails_closed() -> None:
    policy = replace(
        fixture_policy(continue_after_insufficient_funds=True),
        membership=list(MEMBERS),  # type: ignore[arg-type]
    )
    with pytest.raises(KernelUnconfigured, match="immutable tuple"):
        initial_state(policy, SCOPE)


def test_priority_then_stable_strategy_id_is_permutation_deterministic() -> None:
    policy = fixture_policy(opening_budget=1_000, continue_after_insufficient_funds=True)
    alpha = request("alpha-request", MEMBERS[0], priority=2, commitment=500, cost=100)
    beta = request("beta-request", MEMBERS[1], priority=1, commitment=400, cost=100)

    first = allocate(initial_state(policy, SCOPE), policy, (alpha, beta))
    second = allocate(initial_state(policy, SCOPE), policy, (beta, alpha))

    assert first == second
    assert [row.request_id for row in first.decisions] == ["beta-request", "alpha-request"]
    assert [row.status for row in first.decisions] == [
        "reserved",
        "rejected_insufficient_funds",
    ]
    _assert_conservation(first)


def test_priority_tie_breaks_by_stable_strategy_instance_id() -> None:
    policy = fixture_policy(opening_budget=500, continue_after_insufficient_funds=True)
    alpha = request("a", MEMBERS[0], priority=4, commitment=300, cost=0)
    beta = request("b", MEMBERS[1], priority=4, commitment=300, cost=0)

    result = allocate(initial_state(policy, SCOPE), policy, (beta, alpha))

    assert [(row.strategy_instance_id, row.status) for row in result.decisions] == [
        (MEMBERS[0], "reserved"),
        (MEMBERS[1], "rejected_insufficient_funds"),
    ]


def test_whole_request_rejects_without_autoqueue_and_later_cycle_can_resubmit() -> None:
    policy = fixture_policy(opening_budget=500, continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)
    rejected = allocate(
        state,
        policy,
        (request("too-large", MEMBERS[0], commitment=450, cost=100),),
    )

    assert rejected.decisions[-1].status == "rejected_insufficient_funds"
    assert not rejected.reservations
    assert rejected.available == 500

    later = allocate(
        rejected,
        policy,
        (request("resubmitted", MEMBERS[0], commitment=400, cost=50, cycle="cycle-2"),),
        event="later-cycle-event",
        cycle="cycle-2",
    )
    assert later.decisions[-1].status == "reserved"
    assert later.reservations[0].cycle_id == "cycle-2"
    assert later.available == 50


def test_test_only_continue_after_reject_choice_is_explicit() -> None:
    policy = fixture_policy(opening_budget=500, continue_after_insufficient_funds=False)
    rows = (
        request("alpha-expensive", MEMBERS[0], priority=0, commitment=450, cost=100),
        request("beta-small", MEMBERS[1], priority=1, commitment=200, cost=50),
    )

    result = allocate(initial_state(policy, SCOPE), policy, rows)

    assert [row.status for row in result.decisions] == [
        "rejected_insufficient_funds",
        "not_processed_after_insufficient_funds",
    ]
    assert result.available == 500
    assert not result.reservations


def test_reservation_atomically_includes_commitment_and_cost_bounds() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    result = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0], commitment=300, cost=75),),
    )

    assert (result.available, result.reserved) == (625, 375)
    assert result.reservations[0].commitment_remaining == 300
    assert result.reservations[0].cost_remaining == 75
    _assert_conservation(result)


@pytest.mark.parametrize(
    ("commitment", "cost"),
    [(901, 0), (100, 201)],
)
def test_request_over_policy_bound_is_rejected_without_mutation(commitment: int, cost: int) -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)

    with pytest.raises(KernelConflict, match="bound"):
        allocate(state, policy, (request("invalid", MEMBERS[0], commitment=commitment, cost=cost),))
    assert state.revision == 0
    assert state.available == 1_000


@pytest.mark.parametrize("request_id", ["", "   "])
def test_request_requires_nonblank_id_before_cycle_is_committed(request_id: str) -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)

    with pytest.raises(KernelConflict, match="request_id.*non-empty"):
        allocate(state, policy, (request(request_id, MEMBERS[0]),))

    assert state.revision == 0
    assert state.available == state.initial_budget
    assert not state.closed_cycles


def test_fill_requires_nonblank_id_without_mutating_reservation() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )

    with pytest.raises(KernelConflict, match="fill_id.*non-empty"):
        fill(state, policy, event="invalid-fill", fill_id="   ")

    assert state.revision == 1
    assert state.reservations[0].commitment_remaining == 300


def test_cycle_cap_is_one_request_per_strategy_and_no_automatic_second_attempt() -> None:
    policy = fixture_policy(opening_budget=1_000, continue_after_insufficient_funds=True)
    rows = (
        request("alpha-first", MEMBERS[0], priority=0, commitment=100, cost=0),
        request("alpha-second", MEMBERS[0], priority=1, commitment=100, cost=0),
    )

    result = allocate(initial_state(policy, SCOPE), policy, rows)

    assert [row.status for row in result.decisions] == ["reserved", "rejected_request_cap"]
    assert len(result.reservations) == 1
    _assert_conservation(result)


def test_cycle_is_one_final_batch_and_replay_is_idempotent() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)
    low_priority = request("low-priority", MEMBERS[1], priority=8, commitment=100, cost=0)
    first = allocate(state, policy, (low_priority,), event="cycle-final-batch", cycle="cycle-1")
    assert first.closed_cycles[0].cycle_id == "cycle-1"
    assert first.closed_cycles[0].event_id == "cycle-final-batch"
    assert first.closed_cycles[0].batch_fingerprint
    assert first.closed_cycles[0].batch_fingerprint == first.events[-1].payload_digest

    replay = allocate_cycle(
        first,
        policy,
        (low_priority,),
        event_id="cycle-final-batch",
        cycle_id="cycle-1",
        expected_scope=SCOPE,
        expected_revision=state.revision,
    )
    assert replay == first

    higher_priority = request("higher-priority", MEMBERS[0], priority=0, commitment=100, cost=0)
    with pytest.raises(KernelConflict, match="cycle is already closed"):
        allocate(
            first,
            policy,
            (higher_priority,),
            event="late-higher-priority-batch",
            cycle="cycle-1",
        )
    assert first.revision == 1
    assert len(first.decisions) == 1
    assert first.decisions[0].request_id == "low-priority"
    assert first.available == 900


def test_ineligible_member_or_cycle_request_is_not_reserved() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    result = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("ineligible", MEMBERS[0], eligible=False),),
    )
    assert result.decisions[-1].status == "ineligible"
    assert result.available == 1_000
    assert not result.reservations

    with pytest.raises(KernelConflict, match="not eligible"):
        allocate(
            result,
            policy,
            (request("wrong-cycle", MEMBERS[0], cycle="cycle-x"),),
            event="wrong-cycle-event",
            cycle="cycle-x",
        )


def test_partial_fill_moves_reservation_to_exposure_and_consumed_costs() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0], commitment=300, cost=50),),
    )
    filled = fill(state, policy, event="fill-event", fill_id="fill-1", commitment=100, cost=10)

    assert (filled.available, filled.reserved, filled.exposure, filled.consumed_costs) == (
        650,
        240,
        100,
        10,
    )
    reservation = filled.reservations[0]
    assert (reservation.commitment_remaining, reservation.cost_remaining) == (200, 40)
    _assert_conservation(filled)


def test_replayed_fill_is_deduplicated_and_conflicting_event_rejected() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    applied = fill(state, policy, event="fill-event", fill_id="fill-1")
    replay = apply_fill(
        applied,
        policy,
        FillReceipt("fill-1", "request-alpha", MEMBERS[0], SCOPE, 100, 10),
        event_id="fill-event",
        expected_scope=SCOPE,
        expected_revision=state.revision,
    )

    assert replay == applied
    assert replay.revision == applied.revision
    with pytest.raises(KernelConflict, match="event ID replay conflicts"):
        apply_fill(
            applied,
            policy=policy,
            receipt=FillReceipt("fill-1", "request-alpha", MEMBERS[0], SCOPE, 101, 10),
            event_id="fill-event",
            expected_scope=SCOPE,
            expected_revision=state.revision,
        )


def test_allocate_replay_dedupes_after_later_revision_and_permutation() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    rows = (request("request-alpha", MEMBERS[0]),)
    allocated = allocate(initial_state(policy, SCOPE), policy, rows, event="allocate-event")
    filled = fill(allocated, policy, event="fill-event", fill_id="fill-1")

    replay = allocate_cycle(
        filled,
        policy,
        rows,
        event_id="allocate-event",
        cycle_id="cycle-1",
        expected_scope=SCOPE,
        expected_revision=0,
    )
    assert replay == filled


def test_conflicting_scope_generation_revision_and_unit_fail_closed() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)
    row = request("request-alpha", MEMBERS[0])
    with pytest.raises(KernelStale, match="scope/generation/unit"):
        allocate_cycle(
            state,
            policy,
            (row,),
            event_id="bad-scope",
            cycle_id="cycle-1",
            expected_scope=replace(SCOPE, generation="old-writer"),
            expected_revision=0,
        )
    with pytest.raises(KernelStale, match="revision"):
        allocate_cycle(
            state,
            policy,
            (row,),
            event_id="bad-revision",
            cycle_id="cycle-1",
            expected_scope=SCOPE,
            expected_revision=1,
        )
    with pytest.raises(KernelConflict, match="scope or unit"):
        allocate(
            state, policy, (request("wrong-unit", MEMBERS[0], scope=replace(SCOPE, scale=10)),)
        )


def test_fill_cannot_cross_strategy_or_scope_unit() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    with pytest.raises(KernelConflict, match="another strategy"):
        fill(state, policy, event="wrong-strategy", fill_id="fill-x", strategy=MEMBERS[1])
    with pytest.raises(KernelConflict, match="scope or unit"):
        fill(
            state,
            policy,
            event="wrong-unit",
            fill_id="fill-y",
            scope=replace(SCOPE, unit_id="different-unit"),
        )
    assert state.revision == 1


@pytest.mark.parametrize("field", ["request_id", "strategy_instance_id"])
def test_fill_identity_fields_reject_whitespace_without_state_mutation(field: str) -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    receipt = FillReceipt("fill-valid", "request-alpha", MEMBERS[0], SCOPE, 100, 10)
    receipt = replace(receipt, **{field: "   "})

    with pytest.raises(KernelConflict, match=field):
        apply_fill(
            state,
            policy,
            receipt,
            event_id="fill-invalid-identity",
            expected_scope=SCOPE,
            expected_revision=state.revision,
        )

    assert state.revision == 1
    assert state.reservations[0].commitment_remaining == 300
    assert not state.fill_records


@pytest.mark.parametrize("field", ["request_id", "strategy_instance_id"])
def test_cancel_observation_identity_rejects_whitespace_without_state_mutation(
    field: str,
) -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    args = {"request_id": "request-alpha", "strategy_instance_id": MEMBERS[0]}
    args[field] = "   "

    with pytest.raises(KernelConflict, match=field):
        observe_cancel_or_lost_ack(
            state,
            policy,
            event_id="cancel-invalid-identity",
            observation="cancel_observed",
            expected_scope=SCOPE,
            expected_revision=state.revision,
            **args,
        )

    assert state.revision == 1
    assert state.reservations[0].observation == "active"
    assert len(state.reservations) == 1


def test_cancel_and_lost_ack_hold_remainder_until_authoritative_terminal() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0], commitment=300, cost=50),),
    )
    partly_filled = fill(state, policy, event="fill-1-event", fill_id="fill-1")
    cancelled = observe_cancel_or_lost_ack(
        partly_filled,
        policy,
        event_id="cancel-event",
        request_id="request-alpha",
        strategy_instance_id=MEMBERS[0],
        observation="cancel_observed",
        expected_scope=SCOPE,
        expected_revision=partly_filled.revision,
    )
    lost_ack = observe_cancel_or_lost_ack(
        cancelled,
        policy,
        event_id="lost-ack-event",
        request_id="request-alpha",
        strategy_instance_id=MEMBERS[0],
        observation="lost_ack",
        expected_scope=SCOPE,
        expected_revision=cancelled.revision,
    )

    assert lost_ack.reservations[0].observation == "lost_ack"
    assert lost_ack.reserved == 240
    assert lost_ack.available == cancelled.available
    with pytest.raises(KernelConflict, match="authority"):
        _terminal(lost_ack, policy, authority="untrusted")
    released = _terminal(lost_ack, policy)

    assert not released.reservations
    assert released.available == 890
    assert released.exposure == 100
    assert released.consumed_costs == 10
    _assert_conservation(released)


def test_policy_content_fingerprint_prevents_authority_and_policy_replacement() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    changed_policies = (
        replace(policy, terminal_authority_id="fixture-substituted-terminal-authority"),
        replace(policy, rank_authority_id="fixture-substituted-rank-authority"),
        replace(policy, membership=(*MEMBERS, "strategy-delta")),
        replace(policy, max_commitment_units=850),
    )

    for changed_policy in changed_policies:
        before = state
        with pytest.raises(KernelConflict, match="policy identity or scope"):
            _terminal(state, changed_policy)
        assert state == before


def test_overbound_fill_is_retained_unresolved_and_blocks_allocation_until_recovery() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0], commitment=300, cost=50),),
    )
    receipt = FillReceipt("fill-over", "request-alpha", MEMBERS[0], SCOPE, 301, 51)
    recovering = apply_fill(
        state,
        policy,
        receipt,
        event_id="overbound-event",
        expected_scope=SCOPE,
        expected_revision=state.revision,
    )

    assert recovering.recovering
    assert recovering.fill_records[-1].receipt == receipt
    assert recovering.fill_records[-1].status == "unresolved"
    assert recovering.reserved == state.reserved
    _assert_conservation(recovering)
    with pytest.raises(KernelConflict, match="recovering"):
        allocate(
            recovering,
            policy,
            (request("next", MEMBERS[1], cycle="cycle-2"),),
            event="blocked-allocation",
            cycle="cycle-2",
        )
    with pytest.raises(KernelConflict, match="only reconciled recovery"):
        _terminal(recovering, policy)
    with pytest.raises(KernelConflict, match="blocked while.*recovering"):
        observe_cancel_or_lost_ack(
            recovering,
            policy,
            event_id="cancel-during-recovery",
            request_id="request-alpha",
            strategy_instance_id=MEMBERS[0],
            observation="cancel_observed",
            expected_scope=SCOPE,
            expected_revision=recovering.revision,
        )

    with pytest.raises(KernelUnconfigured, match="beyond existing reservations"):
        _terminal(
            recovering,
            policy,
            kind="reconciled_recovery",
            authority=policy.reconciliation_authority_id,
            reconciled_fills=(receipt,),
        )
    assert recovering.recovering
    assert recovering.reservations == state.reservations
    assert recovering.available == state.available
    assert recovering.reserved == state.reserved
    _assert_conservation(recovering)


def test_terminal_replay_is_deduplicated_and_wrong_strategy_receipt_rejected() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    wrong_strategy = TerminalReceipt(
        "terminal-event",
        "request-alpha",
        MEMBERS[1],
        SCOPE,
        "terminal",
        policy.terminal_authority_id or "",
        (),
    )
    with pytest.raises(KernelConflict, match="another strategy"):
        apply_terminal(
            state,
            policy,
            wrong_strategy,
            expected_scope=SCOPE,
            expected_revision=state.revision,
        )

    released = _terminal(state, policy)
    replay = apply_terminal(
        released,
        policy,
        TerminalReceipt(
            "terminal-event",
            "request-alpha",
            MEMBERS[0],
            SCOPE,
            "terminal",
            policy.terminal_authority_id or "",
            (),
        ),
        expected_scope=SCOPE,
        expected_revision=state.revision,
    )
    assert replay == released


@pytest.mark.parametrize(
    "field", ["receipt_id", "authority_id", "request_id", "strategy_instance_id"]
)
def test_terminal_identity_fields_reject_whitespace_without_state_mutation(field: str) -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = allocate(
        initial_state(policy, SCOPE),
        policy,
        (request("request-alpha", MEMBERS[0]),),
    )
    receipt = TerminalReceipt(
        "terminal-valid",
        "request-alpha",
        MEMBERS[0],
        SCOPE,
        "terminal",
        policy.terminal_authority_id or "",
        (),
    )
    receipt = replace(receipt, **{field: "   "})

    with pytest.raises(KernelConflict, match=field):
        apply_terminal(
            state,
            policy,
            receipt,
            expected_scope=SCOPE,
            expected_revision=state.revision,
        )

    assert state.revision == 1
    assert state.available == 650
    assert state.reserved == 350
    assert len(state.reservations) == 1
    assert not state.events[1:]


def test_nonnegative_conservation_invariant_rejects_invalid_state() -> None:
    policy = fixture_policy(continue_after_insufficient_funds=True)
    state = initial_state(policy, SCOPE)

    with pytest.raises(KernelError, match="conservation"):
        replace(state, available=999)
    with pytest.raises(KernelError, match="non-negative"):
        replace(state, exposure=-1)


def _terminal(
    state: AllocationState,
    policy: AllocationPolicy,
    *,
    authority: str | None = None,
    kind: str = "terminal",
    reconciled_fills: tuple[FillReceipt, ...] = (),
) -> AllocationState:
    return apply_terminal(
        state,
        policy,
        TerminalReceipt(
            receipt_id="terminal-event",
            request_id="request-alpha",
            strategy_instance_id=MEMBERS[0],
            scope=SCOPE,
            kind=kind,  # type: ignore[arg-type]
            authority_id=authority or policy.terminal_authority_id or "",
            reconciled_fills=reconciled_fills,
        ),
        expected_scope=SCOPE,
        expected_revision=state.revision,
    )


def _assert_conservation(state: AllocationState) -> None:
    assert state.available >= 0
    assert state.reserved >= 0
    assert state.exposure >= 0
    assert state.consumed_costs >= 0
    assert (
        state.available + state.reserved + state.exposure + state.consumed_costs
        == state.initial_budget
    )
    assert (
        sum(row.commitment_remaining + row.cost_remaining for row in state.reservations)
        == state.reserved
    )
    assert (
        sum(
            row.receipt.commitment_units
            for row in state.fill_records
            if row.status in {"applied", "reconciled"}
        )
        == state.exposure
    )
    assert (
        sum(
            row.receipt.cost_units
            for row in state.fill_records
            if row.status in {"applied", "reconciled"}
        )
        == state.consumed_costs
    )
