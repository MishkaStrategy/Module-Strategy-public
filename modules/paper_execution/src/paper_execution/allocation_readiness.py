"""Inert, fail-closed inputs for future portfolio allocation ownership.

This module only checks that the required authority references are structurally
present and bound to one immutable scope. It does not authenticate their
provenance, choose policy, calculate allocations, reserve capital, or authorize
dispatch. A complete assessment still requires owner review.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


ALLOCATION_INPUT_VERSION = "portfolio-allocation-input-v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AllocationAuthorityKind(StrEnum):
    CAPITAL = "capital"
    CURRENCY = "currency"
    BALANCE = "balance"
    PNL = "pnl"
    COMMITMENT = "commitment"
    COST = "cost"
    OVERFILL_POLICY = "overfill_policy"
    PRIORITY_RANK = "priority_rank"
    CYCLE = "cycle"
    REQUEST_LIMITS = "request_limits"
    TERMINAL = "terminal"
    RECONCILIATION = "reconciliation"


# These are evidence-coverage slots, not a requirement for 12 distinct owners
# or source IDs. One owner/source may attest multiple kinds where its contract says so.
REQUIRED_AUTHORITY_KINDS = tuple(sorted(AllocationAuthorityKind, key=lambda item: item.value))


class PriorityDirection(StrEnum):
    ASCENDING = "ascending"
    DESCENDING = "descending"


class BatchContinuationPolicy(StrEnum):
    CONTINUE = "continue"
    STOP = "stop"


class WholeRequestPolicy(StrEnum):
    REJECT_WHOLE = "reject_whole"


class AllocationReadinessState(StrEnum):
    UNCONFIGURED = "unconfigured"
    BLOCKED = "blocked"
    PRESENT_UNVERIFIED = "present_unverified"


@dataclass(frozen=True, slots=True)
class AllocationScopeBinding:
    """Immutable shared-pool membership identity supplied by its future owner."""

    portfolio_id: str
    pool_id: str
    scope_version: str
    strategy_instance_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class AllocationAuthorityEvidence:
    """Opaque evidence reference with provenance, version, observation, and scope.

    The digest and identifiers provide a structural binding only. The timestamp
    is carried as provenance and is not checked for freshness. This value is not
    an authenticated credential or proof that an authority is trusted.
    """

    kind: AllocationAuthorityKind
    authority_id: str
    source_id: str
    source_version: str
    scope: AllocationScopeBinding
    request_binding: AllocationRequestBinding
    observed_at: datetime
    evidence_sha256: str


@dataclass(frozen=True, slots=True)
class AllocationRequestBinding:
    """Immutable strategy/plan/request identity at one aggregate revision."""

    scope: AllocationScopeBinding
    aggregate_generation: str
    aggregate_version: int
    strategy_instance_id: str
    plan_id: str
    request_id: str
    request_sha256: str


@dataclass(frozen=True, slots=True)
class PortfolioAllocationInputs:
    """Immutable request-bound inputs; absent policy/authority stays absent.

    The fixed REJECT_WHOLE policy governs an underfunded request itself. The
    independent batch-continuation policy remains unconfigured until supplied.
    """

    contract_version: str = ALLOCATION_INPUT_VERSION
    scope: AllocationScopeBinding | None = None
    request_binding: AllocationRequestBinding | None = None
    authority_evidence: tuple[AllocationAuthorityEvidence, ...] = ()
    priority_direction: PriorityDirection | None = None
    tie_breaker: str | None = None
    request_rejection_policy: WholeRequestPolicy = WholeRequestPolicy.REJECT_WHOLE
    batch_continuation_policy: BatchContinuationPolicy | None = None


@dataclass(frozen=True, slots=True)
class AllocationReadinessAssessment:
    state: AllocationReadinessState
    missing_inputs: tuple[str, ...]
    blockers: tuple[str, ...]
    scope: AllocationScopeBinding | None


def assess_allocation_readiness(inputs: object) -> AllocationReadinessAssessment:
    """Purely assess structural completeness; never grants allocation readiness."""

    if type(inputs) is not PortfolioAllocationInputs:
        return AllocationReadinessAssessment(
            AllocationReadinessState.BLOCKED,
            (),
            ("allocation_inputs_type_invalid",),
            None,
        )

    blockers: list[str] = []
    missing: list[str] = []
    if inputs.contract_version != ALLOCATION_INPUT_VERSION:
        blockers.append("allocation_input_contract_version_unsupported")

    scope = inputs.scope
    if scope is None:
        missing.append("scope")
    elif not _valid_scope(scope):
        blockers.append("scope_invalid")

    request_binding = inputs.request_binding
    if request_binding is None:
        missing.append("request_binding")
    elif not _valid_request_binding(request_binding):
        blockers.append("request_binding_invalid")
    elif scope is not None and request_binding.scope != scope:
        blockers.append("request_binding_scope_mismatch")

    authority_evidence: object = inputs.authority_evidence
    if not isinstance(authority_evidence, tuple):
        blockers.append("authority_evidence_not_immutable_tuple")
        evidence_rows: tuple[object, ...] = ()
    else:
        evidence_rows = authority_evidence

    by_kind: dict[AllocationAuthorityKind, AllocationAuthorityEvidence] = {}
    for index, row in enumerate(evidence_rows):
        if type(row) is not AllocationAuthorityEvidence:
            blockers.append(f"authority_evidence_{index}_invalid")
            continue
        if not _valid_evidence(row):
            blockers.append(f"authority_evidence_{index}_invalid")
            continue
        if row.kind in by_kind:
            blockers.append(f"authority_evidence_duplicate_{row.kind.value}")
            continue
        by_kind[row.kind] = row
        if scope is not None and row.scope != scope:
            blockers.append(f"authority_evidence_scope_mismatch_{row.kind.value}")
        if request_binding is None or row.request_binding != request_binding:
            blockers.append(f"authority_evidence_request_binding_mismatch_{row.kind.value}")

    for kind in REQUIRED_AUTHORITY_KINDS:
        if kind not in by_kind:
            missing.append(f"authority:{kind.value}")

    priority_direction: object = inputs.priority_direction
    if priority_direction is None:
        missing.append("priority_direction")
    elif not isinstance(priority_direction, PriorityDirection):
        blockers.append("priority_direction_invalid")

    # The product contract fixes the deterministic tie-break key, but no rank
    # source or direction is selected here.
    if inputs.tie_breaker is None:
        missing.append("tie_breaker")
    elif inputs.tie_breaker != "strategy_instance_id":
        blockers.append("tie_breaker_unsupported")

    request_rejection_policy: object = inputs.request_rejection_policy
    if request_rejection_policy is not WholeRequestPolicy.REJECT_WHOLE:
        blockers.append("request_rejection_policy_must_reject_whole_request")

    # This controls what happens to later batch requests; it is independent from
    # the selected whole-request rejection invariant and deliberately has no default.
    batch_continuation_policy: object = inputs.batch_continuation_policy
    if batch_continuation_policy is None:
        missing.append("batch_continuation_policy")
    elif not isinstance(batch_continuation_policy, BatchContinuationPolicy):
        blockers.append("batch_continuation_policy_invalid")

    if blockers:
        state = AllocationReadinessState.BLOCKED
    elif missing:
        state = AllocationReadinessState.UNCONFIGURED
    else:
        # Presence is not authentication, freshness, or approval. This API cannot authorize
        # reservations, dispatch, release, or production composition.
        state = AllocationReadinessState.PRESENT_UNVERIFIED
    return AllocationReadinessAssessment(
        state=state,
        missing_inputs=tuple(sorted(set(missing))),
        blockers=tuple(sorted(set(blockers))),
        scope=scope if _valid_scope(scope) else None,
    )


def _valid_scope(scope: object) -> bool:
    if type(scope) is not AllocationScopeBinding:
        return False
    return (
        _identifier(scope.portfolio_id)
        and _identifier(scope.pool_id)
        and _identifier(scope.scope_version)
        and isinstance(scope.strategy_instance_ids, tuple)
        and bool(scope.strategy_instance_ids)
        and all(_identifier(item) for item in scope.strategy_instance_ids)
        and scope.strategy_instance_ids == tuple(sorted(set(scope.strategy_instance_ids)))
    )


def _valid_evidence(row: AllocationAuthorityEvidence) -> bool:
    return (
        isinstance(row.kind, AllocationAuthorityKind)
        and _identifier(row.authority_id)
        and _identifier(row.source_id)
        and _identifier(row.source_version)
        and _valid_scope(row.scope)
        and _valid_request_binding(row.request_binding)
        and isinstance(row.observed_at, datetime)
        and row.observed_at.tzinfo is not None
        and row.observed_at.utcoffset() is not None
        and isinstance(row.evidence_sha256, str)
        and _SHA256.fullmatch(row.evidence_sha256) is not None
    )


def _valid_request_binding(binding: object) -> bool:
    return (
        type(binding) is AllocationRequestBinding
        and _valid_scope(binding.scope)
        and _identifier(binding.aggregate_generation)
        and isinstance(binding.aggregate_version, int)
        and not isinstance(binding.aggregate_version, bool)
        and binding.aggregate_version >= 0
        and _identifier(binding.strategy_instance_id)
        and binding.strategy_instance_id in binding.scope.strategy_instance_ids
        and _identifier(binding.plan_id)
        and _identifier(binding.request_id)
        and isinstance(binding.request_sha256, str)
        and _SHA256.fullmatch(binding.request_sha256) is not None
    )


def _identifier(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


__all__ = [
    "ALLOCATION_INPUT_VERSION",
    "REQUIRED_AUTHORITY_KINDS",
    "AllocationAuthorityEvidence",
    "AllocationAuthorityKind",
    "AllocationRequestBinding",
    "AllocationReadinessAssessment",
    "AllocationReadinessState",
    "AllocationScopeBinding",
    "BatchContinuationPolicy",
    "PortfolioAllocationInputs",
    "PriorityDirection",
    "WholeRequestPolicy",
    "assess_allocation_readiness",
]
