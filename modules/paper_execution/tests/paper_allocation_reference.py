"""Test-only pure reference model for a future #396 allocation kernel.

Nothing in the production package imports this module. All integer units,
authorities, bounds, rank direction, cycles, and continuation behavior must be
explicitly supplied by synthetic test fixtures; they are not production policy.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, fields, replace
from typing import Literal, TypeVar


class KernelError(ValueError):
    """A reference-kernel transition is invalid or conflicts with its state."""


class KernelUnconfigured(KernelError):
    """Required policy authority or input is explicitly unconfigured."""


class KernelConflict(KernelError):
    """An event, scope, revision, identity, or accounting fact conflicts."""


class KernelStale(KernelConflict):
    """A transition used a stale aggregate scope or revision."""


@dataclass(frozen=True, slots=True)
class AllocationScope:
    scope_id: str
    generation: str
    unit_id: str
    scale: int

    def __post_init__(self) -> None:
        for name in ("scope_id", "generation", "unit_id"):
            _require_identifier(getattr(self, name), name)
        if not _is_int(self.scale) or self.scale <= 0:
            raise KernelError("allocation scope scale must be a positive integer")


@dataclass(frozen=True, slots=True)
class AllocationPolicy:
    policy_id: str | None
    scope: AllocationScope | None
    opening_budget: int | None
    max_commitment_units: int | None
    max_cost_units: int | None
    priority_direction: Literal["ascending", "descending"] | None
    priority_min: int | None
    priority_max: int | None
    membership: tuple[str, ...] | None
    eligible_cycle_ids: tuple[str, ...] | None
    eligible_members: tuple[str, ...] | None
    max_requests_per_strategy_per_cycle: int | None
    continue_after_insufficient_funds: bool | None
    capital_authority_id: str | None
    commitment_authority_id: str | None
    cost_authority_id: str | None
    rank_authority_id: str | None
    terminal_authority_id: str | None
    reconciliation_authority_id: str | None


@dataclass(frozen=True, slots=True)
class AllocationRequest:
    request_id: str
    strategy_instance_id: str
    cycle_id: str
    scope: AllocationScope
    priority: int
    commitment_units: int
    cost_bound_units: int
    eligible: bool


@dataclass(frozen=True, slots=True)
class Reservation:
    request_id: str
    strategy_instance_id: str
    cycle_id: str
    commitment_remaining: int
    cost_remaining: int
    observation: Literal["active", "cancel_observed", "lost_ack"]


@dataclass(frozen=True, slots=True)
class FillReceipt:
    fill_id: str
    request_id: str
    strategy_instance_id: str
    scope: AllocationScope
    commitment_units: int
    cost_units: int


@dataclass(frozen=True, slots=True)
class FillRecord:
    receipt: FillReceipt
    status: Literal["applied", "unresolved", "reconciled"]


@dataclass(frozen=True, slots=True)
class TerminalReceipt:
    receipt_id: str
    request_id: str
    strategy_instance_id: str
    scope: AllocationScope
    kind: Literal["terminal", "reconciled_recovery"]
    authority_id: str
    reconciled_fills: tuple[FillReceipt, ...]


@dataclass(frozen=True, slots=True)
class AllocationDecision:
    request_id: str
    strategy_instance_id: str
    status: Literal[
        "reserved",
        "rejected_insufficient_funds",
        "rejected_request_cap",
        "ineligible",
        "not_processed_after_insufficient_funds",
    ]
    reserved_units: int


@dataclass(frozen=True, slots=True)
class EventRecord:
    event_id: str
    kind: str
    scope: AllocationScope
    expected_revision: int
    payload_digest: str
    result_revision: int


@dataclass(frozen=True, slots=True)
class ClosedCycle:
    cycle_id: str
    event_id: str
    batch_fingerprint: str


@dataclass(frozen=True, slots=True)
class AllocationState:
    scope: AllocationScope
    policy_id: str
    policy_fingerprint: str
    initial_budget: int
    available: int
    reserved: int
    exposure: int
    consumed_costs: int
    revision: int
    recovering: bool
    reservations: tuple[Reservation, ...]
    cycle_counts: tuple[tuple[str, str, int], ...]
    closed_cycles: tuple[ClosedCycle, ...]
    decisions: tuple[AllocationDecision, ...]
    fill_records: tuple[FillRecord, ...]
    events: tuple[EventRecord, ...]

    def __post_init__(self) -> None:
        amounts = (
            self.initial_budget,
            self.available,
            self.reserved,
            self.exposure,
            self.consumed_costs,
        )
        if any(not _is_int(value) or value < 0 for value in amounts):
            raise KernelError("allocation state amounts must be non-negative integers")
        if not _is_int(self.revision) or self.revision < 0:
            raise KernelError("revision must be a non-negative integer")
        if not self.policy_fingerprint:
            raise KernelError("allocation state requires a policy fingerprint")
        cycle_ids = [row.cycle_id for row in self.closed_cycles]
        if len(cycle_ids) != len(set(cycle_ids)):
            raise KernelError("allocation state contains a duplicate closed cycle")
        if (
            self.available + self.reserved + self.exposure + self.consumed_costs
            != self.initial_budget
        ):
            raise KernelError("allocation state violates budget conservation")
        reservation_total = sum(
            row.commitment_remaining + row.cost_remaining for row in self.reservations
        )
        if reservation_total != self.reserved:
            raise KernelError("reservation rows do not match reserved balance")
        applied = [
            row.receipt for row in self.fill_records if row.status in {"applied", "reconciled"}
        ]
        if sum(row.commitment_units for row in applied) != self.exposure:
            raise KernelError("fill records do not match exposure")
        if sum(row.cost_units for row in applied) != self.consumed_costs:
            raise KernelError("fill records do not match consumed costs")
        if any(row.status == "unresolved" for row in self.fill_records) and not self.recovering:
            raise KernelError("unresolved fills require recovering state")


def initial_state(policy: AllocationPolicy, scope: AllocationScope) -> AllocationState:
    _require_configured(policy)
    if policy.scope != scope or policy.opening_budget is None:
        raise KernelConflict("initial state scope or budget does not match policy")
    return AllocationState(
        scope=scope,
        policy_id=_required(policy.policy_id, "policy_id"),
        policy_fingerprint=_policy_fingerprint(policy),
        initial_budget=policy.opening_budget,
        available=policy.opening_budget,
        reserved=0,
        exposure=0,
        consumed_costs=0,
        revision=0,
        recovering=False,
        reservations=(),
        cycle_counts=(),
        closed_cycles=(),
        decisions=(),
        fill_records=(),
        events=(),
    )


def allocate_cycle(
    state: AllocationState,
    policy: AllocationPolicy,
    requests: tuple[AllocationRequest, ...],
    *,
    event_id: str,
    cycle_id: str,
    expected_scope: AllocationScope,
    expected_revision: int,
) -> AllocationState:
    _prepare(policy, state, event_id, "allocate_cycle", expected_scope, expected_revision)
    _require_identifier(cycle_id, "cycle_id")
    if not isinstance(requests, tuple) or any(
        not isinstance(row, AllocationRequest) for row in requests
    ):
        raise KernelError("requests must be a tuple of AllocationRequest")
    payload_digest = _digest((cycle_id, tuple(sorted(requests, key=_request_key))))
    replay = _replay(
        state, event_id, "allocate_cycle", expected_scope, expected_revision, payload_digest
    )
    if replay:
        return state
    _check_revision(state, expected_revision)
    if state.recovering:
        raise KernelConflict("allocation is blocked while the aggregate is recovering")
    if any(row.cycle_id == cycle_id for row in state.closed_cycles):
        raise KernelConflict("allocation cycle is already closed by a different event")
    if cycle_id not in _required(policy.eligible_cycle_ids, "eligible_cycle_ids"):
        raise KernelConflict("allocation cycle is not eligible under the fixture policy")
    for row in requests:
        _require_identifier(row.request_id, "request_id")
        _require_identifier(row.strategy_instance_id, "strategy_instance_id")
        _require_identifier(row.cycle_id, "cycle_id")
        if row.scope != state.scope:
            raise KernelConflict("request scope or unit conflicts with aggregate scope")
        if row.cycle_id != cycle_id:
            raise KernelConflict("request cycle does not match allocation cycle")
        if row.strategy_instance_id not in _required(policy.membership, "membership"):
            raise KernelConflict("request strategy is outside aggregate membership")
        if not _is_int(row.priority) or not (
            _required(policy.priority_min, "priority_min")
            <= row.priority
            <= _required(policy.priority_max, "priority_max")
        ):
            raise KernelConflict("request priority is outside configured fixture range")
        if (
            not _is_int(row.commitment_units)
            or row.commitment_units <= 0
            or not _is_int(row.cost_bound_units)
            or row.cost_bound_units < 0
        ):
            raise KernelConflict("request commitment/cost must use valid integer units")
        if row.commitment_units > _required(policy.max_commitment_units, "max_commitment_units"):
            raise KernelConflict("request exceeds configured commitment bound")
        if row.cost_bound_units > _required(policy.max_cost_units, "max_cost_units"):
            raise KernelConflict("request exceeds configured cost bound")
    request_ids = [row.request_id for row in requests]
    if len(request_ids) != len(set(request_ids)) or any(
        any(record.event_id == request_id for record in state.events)
        or any(decision.request_id == request_id for decision in state.decisions)
        for request_id in request_ids
    ):
        raise KernelConflict("request ID is duplicated or already recorded")

    ordered = sorted(requests, key=lambda row: _request_key_for_policy(row, policy))
    counts = {(cycle, strategy): count for cycle, strategy, count in state.cycle_counts}
    decisions: list[AllocationDecision] = []
    available = state.available
    reservations = list(state.reservations)
    stopped = False
    eligible_members = set(_required(policy.eligible_members, "eligible_members"))
    cap = _required(
        policy.max_requests_per_strategy_per_cycle, "max_requests_per_strategy_per_cycle"
    )
    for row in ordered:
        if not row.eligible or row.strategy_instance_id not in eligible_members:
            decisions.append(
                AllocationDecision(row.request_id, row.strategy_instance_id, "ineligible", 0)
            )
            continue
        count_key = (cycle_id, row.strategy_instance_id)
        current_count = counts.get(count_key, 0)
        if current_count >= cap:
            decisions.append(
                AllocationDecision(
                    row.request_id, row.strategy_instance_id, "rejected_request_cap", 0
                )
            )
            continue
        counts[count_key] = current_count + 1
        if stopped:
            decisions.append(
                AllocationDecision(
                    row.request_id,
                    row.strategy_instance_id,
                    "not_processed_after_insufficient_funds",
                    0,
                )
            )
            continue
        required = row.commitment_units + row.cost_bound_units
        if required > available:
            decisions.append(
                AllocationDecision(
                    row.request_id, row.strategy_instance_id, "rejected_insufficient_funds", 0
                )
            )
            stopped = not _required(
                policy.continue_after_insufficient_funds, "continue_after_insufficient_funds"
            )
            continue
        available -= required
        reservations.append(
            Reservation(
                request_id=row.request_id,
                strategy_instance_id=row.strategy_instance_id,
                cycle_id=cycle_id,
                commitment_remaining=row.commitment_units,
                cost_remaining=row.cost_bound_units,
                observation="active",
            )
        )
        decisions.append(
            AllocationDecision(row.request_id, row.strategy_instance_id, "reserved", required)
        )
    next_counts = tuple(
        (cycle, strategy, count) for (cycle, strategy), count in sorted(counts.items())
    )
    return _commit(
        state,
        event_id,
        "allocate_cycle",
        expected_scope,
        expected_revision,
        payload_digest,
        available=available,
        reserved=state.reserved + state.available - available,
        reservations=tuple(sorted(reservations, key=lambda row: row.request_id)),
        cycle_counts=next_counts,
        closed_cycles=state.closed_cycles + (ClosedCycle(cycle_id, event_id, payload_digest),),
        decisions=state.decisions + tuple(decisions),
    )


def apply_fill(
    state: AllocationState,
    policy: AllocationPolicy,
    receipt: FillReceipt,
    *,
    event_id: str,
    expected_scope: AllocationScope,
    expected_revision: int,
) -> AllocationState:
    payload = receipt
    _prepare(policy, state, event_id, "apply_fill", expected_scope, expected_revision)
    replay = _replay(
        state, event_id, "apply_fill", expected_scope, expected_revision, _digest(payload)
    )
    if replay:
        return state
    _check_revision(state, expected_revision)
    if state.recovering:
        raise KernelConflict("fill application is blocked while the aggregate is recovering")
    _require_identifier(receipt.fill_id, "fill_id")
    _require_identifier(receipt.request_id, "request_id")
    _require_identifier(receipt.strategy_instance_id, "strategy_instance_id")
    _validate_fill_identity(state, receipt)
    if any(row.receipt.fill_id == receipt.fill_id for row in state.fill_records):
        raise KernelConflict("fill ID is already recorded")
    reservation = _reservation(state, receipt.request_id, receipt.strategy_instance_id)
    if not _is_int(receipt.commitment_units) or receipt.commitment_units <= 0:
        raise KernelConflict("fill commitment must be a positive integer unit count")
    if not _is_int(receipt.cost_units) or receipt.cost_units < 0:
        raise KernelConflict("fill cost must be a non-negative integer unit count")
    within = (
        receipt.commitment_units <= reservation.commitment_remaining
        and receipt.cost_units <= reservation.cost_remaining
    )
    if not within:
        record = FillRecord(receipt, "unresolved")
        return _commit(
            state,
            event_id,
            "apply_fill",
            expected_scope,
            expected_revision,
            _digest(payload),
            recovering=True,
            fill_records=state.fill_records + (record,),
        )
    updated = replace(
        reservation,
        commitment_remaining=reservation.commitment_remaining - receipt.commitment_units,
        cost_remaining=reservation.cost_remaining - receipt.cost_units,
    )
    return _replace_reservation_and_commit(
        state,
        event_id,
        "apply_fill",
        expected_scope,
        expected_revision,
        _digest(payload),
        reservation,
        updated,
        reserved=state.reserved - receipt.commitment_units - receipt.cost_units,
        exposure=state.exposure + receipt.commitment_units,
        consumed_costs=state.consumed_costs + receipt.cost_units,
        fill_records=state.fill_records + (FillRecord(receipt, "applied"),),
    )


def observe_cancel_or_lost_ack(
    state: AllocationState,
    policy: AllocationPolicy,
    *,
    event_id: str,
    request_id: str,
    strategy_instance_id: str,
    observation: Literal["cancel_observed", "lost_ack"],
    expected_scope: AllocationScope,
    expected_revision: int,
) -> AllocationState:
    _require_identifier(request_id, "request_id")
    _require_identifier(strategy_instance_id, "strategy_instance_id")
    payload = (request_id, strategy_instance_id, observation)
    _prepare(
        policy, state, event_id, "observe_cancel_or_lost_ack", expected_scope, expected_revision
    )
    replay = _replay(
        state,
        event_id,
        "observe_cancel_or_lost_ack",
        expected_scope,
        expected_revision,
        _digest(payload),
    )
    if replay:
        return state
    _check_revision(state, expected_revision)
    if state.recovering:
        raise KernelConflict(
            "reservation observations are blocked while the aggregate is recovering"
        )
    reservation = _reservation(state, request_id, strategy_instance_id)
    updated = replace(reservation, observation=observation)
    return _replace_reservation_and_commit(
        state,
        event_id,
        "observe_cancel_or_lost_ack",
        expected_scope,
        expected_revision,
        _digest(payload),
        reservation,
        updated,
    )


def apply_terminal(
    state: AllocationState,
    policy: AllocationPolicy,
    receipt: TerminalReceipt,
    *,
    expected_scope: AllocationScope,
    expected_revision: int,
) -> AllocationState:
    _require_identifier(receipt.receipt_id, "receipt_id")
    _require_identifier(receipt.request_id, "request_id")
    _require_identifier(receipt.strategy_instance_id, "strategy_instance_id")
    _require_identifier(receipt.authority_id, "authority_id")
    if not isinstance(receipt.reconciled_fills, tuple):
        raise KernelConflict("reconciled fills must be an immutable tuple")
    payload = receipt
    _prepare(policy, state, receipt.receipt_id, "apply_terminal", expected_scope, expected_revision)
    replay = _replay(
        state,
        receipt.receipt_id,
        "apply_terminal",
        expected_scope,
        expected_revision,
        _digest(payload),
    )
    if replay:
        return state
    _check_revision(state, expected_revision)
    if state.recovering and receipt.kind != "reconciled_recovery":
        raise KernelConflict(
            "only reconciled recovery is allowed while the aggregate is recovering"
        )
    reservation = _reservation(state, receipt.request_id, receipt.strategy_instance_id)
    if receipt.scope != state.scope:
        raise KernelConflict("terminal receipt scope or unit conflicts with aggregate scope")
    aggregate_unresolved = tuple(
        row.receipt for row in state.fill_records if row.status == "unresolved"
    )
    unresolved = tuple(row for row in aggregate_unresolved if row.request_id == receipt.request_id)
    if receipt.kind == "terminal":
        if receipt.authority_id != policy.terminal_authority_id:
            raise KernelConflict("terminal receipt authority does not match policy")
        if unresolved or receipt.reconciled_fills:
            raise KernelConflict("unresolved fills require reconciled recovery before release")
    elif receipt.kind == "reconciled_recovery":
        if receipt.authority_id != policy.reconciliation_authority_id:
            raise KernelConflict("reconciliation receipt authority does not match policy")
        if len(unresolved) != len(aggregate_unresolved):
            raise KernelConflict(
                "recovery receipt must resolve all unresolved fills in the aggregate"
            )
        if tuple(sorted(unresolved, key=lambda row: row.fill_id)) != tuple(
            sorted(receipt.reconciled_fills, key=lambda row: row.fill_id)
        ):
            raise KernelConflict("reconciliation receipt does not exactly resolve unresolved fills")
    else:
        raise KernelConflict("unsupported terminal receipt kind")

    available = state.available
    reserved = state.reserved
    exposure = state.exposure
    consumed_costs = state.consumed_costs
    reservations = list(state.reservations)
    fill_records = list(state.fill_records)
    if unresolved:
        total_commitment = sum(row.commitment_units for row in unresolved)
        total_cost = sum(row.cost_units for row in unresolved)
        if (
            total_commitment > reservation.commitment_remaining
            or total_cost > reservation.cost_remaining
        ):
            raise KernelUnconfigured(
                "recovery requires an owner decision for fills beyond existing reservations"
            )
        from_reserved_commitment = total_commitment
        from_reserved_cost = total_cost
        reserved -= from_reserved_commitment + from_reserved_cost
        exposure += total_commitment
        consumed_costs += total_cost
        updated = replace(
            reservation,
            commitment_remaining=reservation.commitment_remaining - from_reserved_commitment,
            cost_remaining=reservation.cost_remaining - from_reserved_cost,
        )
        reservations = _replacement(reservations, reservation, updated)
        resolved_ids = {row.fill_id for row in unresolved}
        fill_records = [
            FillRecord(row.receipt, "reconciled") if row.receipt.fill_id in resolved_ids else row
            for row in fill_records
        ]
        reservation = updated
    remainder = reservation.commitment_remaining + reservation.cost_remaining
    available += remainder
    reserved -= remainder
    reservations = [row for row in reservations if row.request_id != receipt.request_id]
    return _commit(
        state,
        receipt.receipt_id,
        "apply_terminal",
        expected_scope,
        expected_revision,
        _digest(payload),
        available=available,
        reserved=reserved,
        exposure=exposure,
        consumed_costs=consumed_costs,
        reservations=tuple(reservations),
        fill_records=tuple(fill_records),
        recovering=any(row.status == "unresolved" for row in fill_records),
    )


def _prepare(
    policy: AllocationPolicy,
    state: AllocationState,
    event_id: str,
    kind: str,
    expected_scope: AllocationScope,
    expected_revision: int,
) -> None:
    _require_configured(policy)
    _require_identifier(event_id, "event_id")
    if (
        policy.policy_id != state.policy_id
        or policy.scope != state.scope
        or _policy_fingerprint(policy) != state.policy_fingerprint
    ):
        raise KernelConflict("policy identity or scope conflicts with state")
    if expected_scope != state.scope:
        raise KernelStale("expected scope/generation/unit does not match aggregate")
    if not _is_int(expected_revision):
        raise KernelStale("expected revision must be an integer")
    if kind not in {"allocate_cycle", "apply_fill", "observe_cancel_or_lost_ack", "apply_terminal"}:
        raise KernelError("unknown event kind")


def _check_revision(state: AllocationState, expected_revision: int) -> None:
    if expected_revision != state.revision:
        raise KernelStale("expected revision does not match aggregate")


def _require_configured(policy: AllocationPolicy) -> None:
    fields = (
        "policy_id",
        "scope",
        "opening_budget",
        "max_commitment_units",
        "max_cost_units",
        "priority_direction",
        "priority_min",
        "priority_max",
        "membership",
        "eligible_cycle_ids",
        "eligible_members",
        "max_requests_per_strategy_per_cycle",
        "continue_after_insufficient_funds",
        "capital_authority_id",
        "commitment_authority_id",
        "cost_authority_id",
        "rank_authority_id",
        "terminal_authority_id",
        "reconciliation_authority_id",
    )
    for name in fields:
        value = getattr(policy, name)
        if value is None or value == "" or value == ():
            raise KernelUnconfigured(f"policy input {name} is unconfigured")
    for name in ("membership", "eligible_cycle_ids", "eligible_members"):
        value = getattr(policy, name)
        if not isinstance(value, tuple):
            raise KernelUnconfigured(f"policy input {name} must be an immutable tuple")
        if any(not isinstance(item, str) for item in value):
            raise KernelUnconfigured(f"policy input {name} must contain immutable string IDs")
        for item in value:
            _require_identifier(item, name)
    if not isinstance(policy.scope, AllocationScope):
        raise KernelUnconfigured("policy scope is unconfigured")
    for name in (
        "policy_id",
        "capital_authority_id",
        "commitment_authority_id",
        "cost_authority_id",
        "rank_authority_id",
        "terminal_authority_id",
        "reconciliation_authority_id",
    ):
        _require_identifier(getattr(policy, name), name)
    opening_budget = _required(policy.opening_budget, "opening_budget")
    max_commitment = _required(policy.max_commitment_units, "max_commitment_units")
    max_cost = _required(policy.max_cost_units, "max_cost_units")
    priority_min = _required(policy.priority_min, "priority_min")
    priority_max = _required(policy.priority_max, "priority_max")
    membership = _required(policy.membership, "membership")
    eligible_members = _required(policy.eligible_members, "eligible_members")
    eligible_cycle_ids = _required(policy.eligible_cycle_ids, "eligible_cycle_ids")
    if not _is_int(opening_budget) or opening_budget <= 0:
        raise KernelUnconfigured("policy opening budget must be nonzero synthetic integer units")
    if not _is_int(max_commitment) or max_commitment <= 0:
        raise KernelUnconfigured("commitment bound is unconfigured")
    if not _is_int(max_cost) or max_cost < 0:
        raise KernelUnconfigured("cost bound is unconfigured")
    if policy.priority_direction not in {"ascending", "descending"}:
        raise KernelUnconfigured("priority direction is unconfigured")
    if not _is_int(priority_min) or not _is_int(priority_max) or priority_min > priority_max:
        raise KernelUnconfigured("priority range is unconfigured")
    if policy.max_requests_per_strategy_per_cycle != 1:
        raise KernelUnconfigured(
            "reference fixture must set exactly one request per strategy/cycle"
        )
    if not isinstance(policy.continue_after_insufficient_funds, bool):
        raise KernelUnconfigured("continue-after-insufficient-funds behavior is unconfigured")
    if not membership or not set(eligible_members).issubset(membership):
        raise KernelUnconfigured("membership or cycle eligibility is unconfigured")
    if not eligible_cycle_ids:
        raise KernelUnconfigured("eligible cycles are unconfigured")


def _replay(
    state: AllocationState,
    event_id: str,
    kind: str,
    scope: AllocationScope,
    expected_revision: int,
    payload_digest: str,
) -> bool:
    prior = next((row for row in state.events if row.event_id == event_id), None)
    if prior is None:
        return False
    if (
        prior.kind != kind
        or prior.scope != scope
        or prior.expected_revision != expected_revision
        or prior.payload_digest != payload_digest
    ):
        raise KernelConflict("event ID replay conflicts with recorded scope, revision, or payload")
    return True


def _commit(
    state: AllocationState,
    event_id: str,
    kind: str,
    scope: AllocationScope,
    expected_revision: int,
    payload_digest: str,
    *,
    available: int | None = None,
    reserved: int | None = None,
    exposure: int | None = None,
    consumed_costs: int | None = None,
    recovering: bool | None = None,
    reservations: tuple[Reservation, ...] | None = None,
    cycle_counts: tuple[tuple[str, str, int], ...] | None = None,
    decisions: tuple[AllocationDecision, ...] | None = None,
    fill_records: tuple[FillRecord, ...] | None = None,
    closed_cycles: tuple[ClosedCycle, ...] | None = None,
) -> AllocationState:
    result_revision = state.revision + 1
    event = EventRecord(event_id, kind, scope, expected_revision, payload_digest, result_revision)
    return replace(
        state,
        available=state.available if available is None else available,
        reserved=state.reserved if reserved is None else reserved,
        exposure=state.exposure if exposure is None else exposure,
        consumed_costs=state.consumed_costs if consumed_costs is None else consumed_costs,
        recovering=state.recovering if recovering is None else recovering,
        reservations=state.reservations if reservations is None else reservations,
        cycle_counts=state.cycle_counts if cycle_counts is None else cycle_counts,
        decisions=state.decisions if decisions is None else decisions,
        fill_records=state.fill_records if fill_records is None else fill_records,
        closed_cycles=state.closed_cycles if closed_cycles is None else closed_cycles,
        revision=result_revision,
        events=state.events + (event,),
    )


def _replace_reservation_and_commit(
    state: AllocationState,
    event_id: str,
    kind: str,
    scope: AllocationScope,
    expected_revision: int,
    payload_digest: str,
    old: Reservation,
    new: Reservation,
    *,
    available: int | None = None,
    reserved: int | None = None,
    exposure: int | None = None,
    consumed_costs: int | None = None,
    recovering: bool | None = None,
    cycle_counts: tuple[tuple[str, str, int], ...] | None = None,
    decisions: tuple[AllocationDecision, ...] | None = None,
    fill_records: tuple[FillRecord, ...] | None = None,
) -> AllocationState:
    reservations = _replacement(list(state.reservations), old, new)
    return _commit(
        state,
        event_id,
        kind,
        scope,
        expected_revision,
        payload_digest,
        reservations=tuple(sorted(reservations, key=lambda row: row.request_id)),
        available=available,
        reserved=reserved,
        exposure=exposure,
        consumed_costs=consumed_costs,
        recovering=recovering,
        cycle_counts=cycle_counts,
        decisions=decisions,
        fill_records=fill_records,
    )


def _replacement(rows: list[Reservation], old: Reservation, new: Reservation) -> list[Reservation]:
    return [new if row.request_id == old.request_id else row for row in rows]


def _reservation(state: AllocationState, request_id: str, strategy_instance_id: str) -> Reservation:
    row = next((item for item in state.reservations if item.request_id == request_id), None)
    if row is None or row.strategy_instance_id != strategy_instance_id:
        raise KernelConflict("reservation is missing or belongs to another strategy")
    return row


def _validate_fill_identity(state: AllocationState, receipt: FillReceipt) -> None:
    if receipt.scope != state.scope:
        raise KernelConflict("fill receipt scope or unit conflicts with aggregate scope")
    _reservation(state, receipt.request_id, receipt.strategy_instance_id)


def _request_key(row: AllocationRequest) -> tuple[int, str, str]:
    return row.priority, row.strategy_instance_id, row.request_id


def _request_key_for_policy(
    row: AllocationRequest, policy: AllocationPolicy
) -> tuple[int, str, str]:
    priority = row.priority if policy.priority_direction == "ascending" else -row.priority
    return priority, row.strategy_instance_id, row.request_id


def _digest(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _policy_fingerprint(policy: AllocationPolicy) -> str:
    return _digest(tuple((item.name, getattr(policy, item.name)) for item in fields(policy)))


_T = TypeVar("_T")


def _required(value: _T | None, name: str) -> _T:
    if value is None:
        raise KernelUnconfigured(f"policy input {name} is unconfigured")
    return value


def _require_identifier(value: object, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise KernelConflict(f"{name} must be a non-empty string identifier")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)
