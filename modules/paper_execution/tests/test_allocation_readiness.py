from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime

from paper_execution.allocation_readiness import (
    REQUIRED_AUTHORITY_KINDS,
    AllocationAuthorityEvidence,
    AllocationAuthorityKind,
    AllocationReadinessState,
    AllocationRequestBinding,
    AllocationScopeBinding,
    BatchContinuationPolicy,
    PortfolioAllocationInputs,
    PriorityDirection,
    WholeRequestPolicy,
    assess_allocation_readiness,
)


NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


def _scope() -> AllocationScopeBinding:
    return AllocationScopeBinding(
        portfolio_id="portfolio-synthetic",
        pool_id="pool-synthetic",
        scope_version="scope-v1",
        strategy_instance_ids=("instance-a", "instance-b"),
    )


def _request_binding(
    *,
    scope: AllocationScopeBinding | None = None,
    aggregate_generation: str = "aggregate-generation-7",
    aggregate_version: int = 14,
    strategy_instance_id: str = "instance-a",
    plan_id: str = "plan-synthetic",
    request_id: str = "request-synthetic",
    request_sha256: str = "a" * 64,
) -> AllocationRequestBinding:
    return AllocationRequestBinding(
        scope=scope or _scope(),
        aggregate_generation=aggregate_generation,
        aggregate_version=aggregate_version,
        strategy_instance_id=strategy_instance_id,
        plan_id=plan_id,
        request_id=request_id,
        request_sha256=request_sha256,
    )


def _evidence(
    kind: AllocationAuthorityKind,
    *,
    scope: AllocationScopeBinding | None = None,
    request_binding: AllocationRequestBinding | None = None,
) -> AllocationAuthorityEvidence:
    bound_request = request_binding or _request_binding(scope=scope)
    return AllocationAuthorityEvidence(
        kind=kind,
        authority_id=f"authority-{kind.value}",
        source_id=f"source-{kind.value}",
        source_version="source-version-synthetic",
        scope=scope or bound_request.scope,
        request_binding=bound_request,
        observed_at=NOW,
        evidence_sha256=f"{int(kind.value.encode().hex()[:8], 16):064x}",
    )


def _configured() -> PortfolioAllocationInputs:
    binding = _request_binding()
    return PortfolioAllocationInputs(
        scope=_scope(),
        request_binding=binding,
        authority_evidence=tuple(
            _evidence(kind, request_binding=binding) for kind in REQUIRED_AUTHORITY_KINDS
        ),
        priority_direction=PriorityDirection.DESCENDING,
        tie_breaker="strategy_instance_id",
        batch_continuation_policy=BatchContinuationPolicy.STOP,
    )


def test_empty_inputs_are_unconfigured_and_name_missing_authorities() -> None:
    assessment = assess_allocation_readiness(PortfolioAllocationInputs())

    assert assessment.state is AllocationReadinessState.UNCONFIGURED
    assert "scope" in assessment.missing_inputs
    assert "authority:capital" in assessment.missing_inputs
    assert "authority:terminal" in assessment.missing_inputs
    assert "request_binding" in assessment.missing_inputs
    assert "priority_direction" in assessment.missing_inputs
    assert "batch_continuation_policy" in assessment.missing_inputs
    assert not assessment.blockers


def test_complete_synthetic_refs_remain_present_unverified() -> None:
    inputs = _configured()
    before = inputs

    assessment = assess_allocation_readiness(inputs)

    assert assessment.state is AllocationReadinessState.PRESENT_UNVERIFIED
    assert assessment.missing_inputs == ()
    assert assessment.blockers == ()
    assert assessment.scope == _scope()
    assert inputs.request_rejection_policy is WholeRequestPolicy.REJECT_WHOLE
    assert inputs.batch_continuation_policy is BatchContinuationPolicy.STOP
    assert inputs is before


def test_whole_request_rejection_is_independent_of_batch_continuation() -> None:
    stop_after_underfunded = assess_allocation_readiness(_configured())
    continue_after_underfunded = assess_allocation_readiness(
        replace(_configured(), batch_continuation_policy=BatchContinuationPolicy.CONTINUE)
    )

    assert stop_after_underfunded.state is AllocationReadinessState.PRESENT_UNVERIFIED
    assert continue_after_underfunded.state is AllocationReadinessState.PRESENT_UNVERIFIED
    assert _configured().request_rejection_policy is WholeRequestPolicy.REJECT_WHOLE
    assert _configured().batch_continuation_policy is BatchContinuationPolicy.STOP
    assert (
        replace(
            _configured(), batch_continuation_policy=BatchContinuationPolicy.CONTINUE
        ).request_rejection_policy
        is WholeRequestPolicy.REJECT_WHOLE
    )


def test_request_rejection_invariant_cannot_be_changed() -> None:
    assessment = assess_allocation_readiness(
        replace(_configured(), request_rejection_policy="continue_partial")  # type: ignore[arg-type]
    )

    assert assessment.state is AllocationReadinessState.BLOCKED
    assert "request_rejection_policy_must_reject_whole_request" in assessment.blockers


def test_missing_one_required_authority_stays_unconfigured() -> None:
    inputs = _configured()
    inputs = replace(
        inputs,
        authority_evidence=tuple(
            row for row in inputs.authority_evidence if row.kind is not AllocationAuthorityKind.COST
        ),
    )

    assessment = assess_allocation_readiness(inputs)

    assert assessment.state is AllocationReadinessState.UNCONFIGURED
    assert assessment.missing_inputs == ("authority:cost",)


def test_wrong_scope_duplicate_or_invalid_authority_blocks_assessment() -> None:
    inputs = _configured()
    foreign_scope = replace(_scope(), scope_version="other-scope-version")
    rows = list(inputs.authority_evidence)
    rows[0] = _evidence(rows[0].kind, scope=foreign_scope)
    rows.append(rows[1])
    rows.append(object())  # type: ignore[arg-type]

    assessment = assess_allocation_readiness(replace(inputs, authority_evidence=tuple(rows)))

    assert assessment.state is AllocationReadinessState.BLOCKED
    assert any(item.startswith("authority_evidence_scope_mismatch_") for item in assessment.blockers)
    assert any(item.startswith("authority_evidence_duplicate_") for item in assessment.blockers)
    assert "authority_evidence_13_invalid" in assessment.blockers


def test_foreign_plan_or_request_binding_blocks_commitment_evidence() -> None:
    inputs = _configured()
    foreign_binding = replace(
        _request_binding(), plan_id="other-plan", request_id="other-request"
    )
    rows = tuple(
        _evidence(row.kind, request_binding=foreign_binding)
        if row.kind is AllocationAuthorityKind.COMMITMENT
        else row
        for row in inputs.authority_evidence
    )

    assessment = assess_allocation_readiness(replace(inputs, authority_evidence=rows))

    assert assessment.state is AllocationReadinessState.BLOCKED
    assert "authority_evidence_request_binding_mismatch_commitment" in assessment.blockers


def test_request_binding_must_match_aggregate_scope_and_version() -> None:
    inputs = _configured()
    wrong_scope = replace(_scope(), pool_id="other-pool")
    foreign_aggregate = _request_binding(
        scope=wrong_scope, aggregate_generation="aggregate-generation-other"
    )

    scope_result = assess_allocation_readiness(replace(inputs, request_binding=foreign_aggregate))
    wrong_version = replace(
        inputs.request_binding,
        aggregate_version=inputs.request_binding.aggregate_version + 1,
    )
    version_result = assess_allocation_readiness(replace(inputs, request_binding=wrong_version))

    assert scope_result.state is AllocationReadinessState.BLOCKED
    assert "request_binding_scope_mismatch" in scope_result.blockers
    assert version_result.state is AllocationReadinessState.BLOCKED
    assert any(
        item.startswith("authority_evidence_request_binding_mismatch_")
        for item in version_result.blockers
    )


def test_invalid_policy_input_blocks_without_defaulting() -> None:
    inputs = replace(
        _configured(),
        priority_direction="ascending",  # type: ignore[arg-type]
        batch_continuation_policy=None,
    )

    assessment = assess_allocation_readiness(inputs)

    assert assessment.state is AllocationReadinessState.BLOCKED
    assert "priority_direction_invalid" in assessment.blockers
    assert "batch_continuation_policy" in assessment.missing_inputs


def test_inputs_are_immutable_and_wrong_bundle_type_is_blocked() -> None:
    inputs = _configured()
    try:
        inputs.tie_breaker = "request_id"  # type: ignore[misc]
    except FrozenInstanceError:
        pass
    else:  # pragma: no cover - protects the immutable input boundary
        raise AssertionError("allocation inputs must be frozen")

    assert assess_allocation_readiness(object()).state is AllocationReadinessState.BLOCKED
