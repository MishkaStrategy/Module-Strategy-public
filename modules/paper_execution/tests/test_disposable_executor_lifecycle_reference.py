from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest

from disposable_executor_lifecycle_reference import (
    AuthenticatedLease,
    CleanupObservation,
    FixedWorkload,
    InMemoryLifecycleStore,
    LifecycleConflict,
    LifecycleError,
    LifecycleReplayConflict,
    LifecycleRecord,
    LifecycleStale,
    LifecycleState,
    ResourceIncarnation,
)


BASE = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)
CONTROLLER = "fixture-controller"
FENCE = 7
ADMISSION = "fixture-admission"
SCOPE = "sha256-fixture-scope"
PROVISION_OP = "fixture-provision-intent"
RUN_OP = "fixture-run-intent"
DESTROY_OP = "fixture-destroy-intent"
EXPECTED_OBSERVER = "fixture-independent-inventory-observer"


@dataclass
class Fixture:
    store: InMemoryLifecycleStore
    lease: AuthenticatedLease
    workload: FixedWorkload


def fixture(admission_id: str = ADMISSION) -> Fixture:
    lease = AuthenticatedLease(
        admission_id=admission_id,
        controller_id=CONTROLLER,
        fence=FENCE,
        principal_id="fixture-authenticated-principal",
        lease_id="fixture-preclaim-lease",
        created_at=BASE,
        claimed_at=BASE + timedelta(seconds=1),
        execution_deadline=BASE + timedelta(seconds=20),
    )
    workload = FixedWorkload(
        workload_id="fixed-fixture-workload-v1",
        workload_digest="sha256-fixture-workload",
        scope_digest=SCOPE,
        required_output_paths=("report.json", "result.bin"),
        expected_cleanup_observer_id=EXPECTED_OBSERVER,
        max_output_bytes=32,
    )
    store = InMemoryLifecycleStore(CONTROLLER, FENCE)
    store.claim(
        lease,
        workload,
        controller_id=CONTROLLER,
        fence=FENCE,
        operation_id="fixture-claim-operation",
    )
    return Fixture(store, lease, workload)


def advance(
    subject: Fixture,
    method: str,
    operation_id: str,
    at: datetime,
    **inputs: object,
) -> LifecycleRecord:
    record = subject.store.get(subject.lease.admission_id)
    return cast(
        LifecycleRecord,
        getattr(subject.store, method)(
            subject.lease.admission_id,
            controller_id=CONTROLLER,
            fence=FENCE,
            expected_revision=record.revision,
            operation_id=operation_id,
            now=at,
            **inputs,
        ),
    )


def resource(
    *,
    provision_operation_id: str = PROVISION_OP,
    scope_digest: str = SCOPE,
    incarnation_id: str = "fixture-incarnation-1",
    owned_resource_ids: tuple[str, ...] = ("fixture-disk", "fixture-resource-vm"),
    provisioned_at: datetime = BASE + timedelta(seconds=3),
) -> ResourceIncarnation:
    return ResourceIncarnation(
        resource_id="fixture-resource-vm",
        incarnation_id=incarnation_id,
        provision_operation_id=provision_operation_id,
        scope_digest=scope_digest,
        owned_resource_ids=owned_resource_ids,
        provisioned_at=provisioned_at,
    )


def provision(subject: Fixture) -> ResourceIncarnation:
    advance(
        subject,
        "begin_provision",
        "fixture-provision-intent",
        BASE + timedelta(seconds=2),
    )
    result = resource()
    advance(
        subject,
        "observe_provision",
        "fixture-provision-observation",
        BASE + timedelta(seconds=4),
        provision_operation_id=PROVISION_OP,
        resource=result,
        status="acknowledged",
    )
    return result


def scope_verified(subject: Fixture, result: ResourceIncarnation) -> None:
    advance(
        subject,
        "verify_scope",
        "fixture-scope-verification",
        BASE + timedelta(seconds=5),
        resource_incarnation_id=result.incarnation_id,
        observed_scope_digest=SCOPE,
    )


def running(subject: Fixture, result: ResourceIncarnation) -> None:
    scope_verified(subject, result)
    advance(subject, "begin_run", "fixture-run-intent", BASE + timedelta(seconds=6))
    advance(
        subject,
        "observe_run",
        "fixture-run-observation",
        BASE + timedelta(seconds=7),
        run_operation_id=RUN_OP,
        status="acknowledged",
    )


def terminal_with_outputs(subject: Fixture, result: ResourceIncarnation) -> None:
    running(subject, result)
    advance(
        subject,
        "workload_terminal",
        "fixture-workload-terminal",
        BASE + timedelta(seconds=8),
        outcome="passed",
    )
    advance(
        subject,
        "seal_outputs",
        "fixture-output-seal",
        BASE + timedelta(seconds=9),
        outputs={"report.json": b"{}", "result.bin": b"abc"},
    )


def cleanup_observation(
    subject: Fixture,
    result: ResourceIncarnation | None,
    *,
    observed_at: datetime,
    observer_id: str = "fixture-independent-inventory-observer",
    provision_operation_id: str = PROVISION_OP,
    scope_digest: str = SCOPE,
    remaining_resource_ids: tuple[str, ...] = (),
) -> CleanupObservation:
    return CleanupObservation(
        observer_id=observer_id,
        provision_operation_id=provision_operation_id,
        scope_digest=scope_digest,
        resource=result,
        remaining_resource_ids=remaining_resource_ids,
        observed_at=observed_at,
    )


def verify_cleanup(subject: Fixture, observation: CleanupObservation) -> LifecycleRecord:
    record = subject.store.get(subject.lease.admission_id)
    return subject.store.verify_cleanup(
        subject.lease.admission_id,
        observation,
        controller_id=CONTROLLER,
        fence=FENCE,
        expected_revision=record.revision,
        operation_id=f"cleanup-check-{record.revision}",
    )


def test_success_requires_independent_cleanup_after_destroy_ack() -> None:
    subject = fixture()
    result = provision(subject)
    terminal_with_outputs(subject, result)
    sealed = subject.store.get(ADMISSION)

    assert sealed.state is LifecycleState.OUTPUTS_SEALED
    assert sealed.success_available is False
    assert sealed.external_trust_established is False
    assert sealed.acceptance_eligible is False

    advance(subject, "begin_destroy", "fixture-destroy-intent", BASE + timedelta(seconds=10))
    acknowledged = advance(
        subject,
        "observe_destroy_ack",
        "fixture-destroy-ack",
        BASE + timedelta(seconds=11),
        destroy_operation_id=DESTROY_OP,
        status="acknowledged",
    )
    assert acknowledged.state is LifecycleState.DESTROY_INTENT
    assert acknowledged.destroy_acknowledged is True
    assert acknowledged.success_available is False

    rejected_observer = verify_cleanup(
        subject,
        cleanup_observation(
            subject,
            result,
            observer_id="unknown-observer",
            observed_at=BASE + timedelta(seconds=12),
        ),
    )
    assert rejected_observer.state is LifecycleState.CLEANUP_UNCERTAIN
    assert rejected_observer.success_available is False

    verified = verify_cleanup(
        subject,
        cleanup_observation(subject, result, observed_at=BASE + timedelta(seconds=13)),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED
    assert verified.success_available is False

    finalized = advance(
        subject,
        "finalize",
        "fixture-finalize",
        subject.lease.execution_deadline + timedelta(seconds=1),
    )
    assert finalized.state is LifecycleState.FINALIZED
    assert finalized.final_outcome == "passed"
    assert finalized.success_available is True
    assert finalized.external_trust_established is False
    assert finalized.acceptance_eligible is False


def test_ambiguous_provision_is_consumed_and_can_only_finalize_indeterminate() -> None:
    subject = fixture()
    advance(subject, "begin_provision", "fixture-provision-intent", BASE + timedelta(seconds=2))
    uncertain = advance(
        subject,
        "observe_provision",
        "fixture-provision-ambiguous",
        BASE + timedelta(seconds=3),
        provision_operation_id=PROVISION_OP,
        resource=None,
        status="ambiguous",
    )
    assert uncertain.state is LifecycleState.RECOVERY_REQUIRED
    assert uncertain.success_available is False

    advance(subject, "begin_destroy", "fixture-destroy-intent", BASE + timedelta(seconds=21))
    verified = verify_cleanup(
        subject,
        cleanup_observation(subject, None, observed_at=BASE + timedelta(seconds=22)),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED
    finalized = advance(subject, "finalize", "fixture-finalize", BASE + timedelta(seconds=23))
    assert finalized.final_outcome == "indeterminate"
    assert finalized.success_available is False


def test_provision_resource_must_be_created_between_intent_and_observation() -> None:
    for admission_id, provisioned_at in (
        ("fixture-before-provision-intent", BASE + timedelta(milliseconds=1500)),
        ("fixture-after-provision-observation", BASE + timedelta(seconds=5)),
    ):
        subject = fixture(admission_id)
        advance(
            subject,
            "begin_provision",
            "fixture-provision-intent",
            BASE + timedelta(seconds=2),
        )
        rejected = advance(
            subject,
            "observe_provision",
            "fixture-provision-observation",
            BASE + timedelta(seconds=4),
            provision_operation_id=PROVISION_OP,
            resource=resource(provisioned_at=provisioned_at),
            status="acknowledged",
        )
        assert rejected.state is LifecycleState.RECOVERY_REQUIRED
        assert rejected.success_available is False


def test_ambiguous_run_cannot_be_retried_and_cleanup_is_allowed_after_deadline() -> None:
    subject = fixture()
    result = provision(subject)
    scope_verified(subject, result)
    advance(subject, "begin_run", "fixture-run-intent", BASE + timedelta(seconds=6))
    uncertain = advance(
        subject,
        "observe_run",
        "fixture-run-ambiguous",
        BASE + timedelta(seconds=7),
        run_operation_id=RUN_OP,
        status="ambiguous",
    )
    assert uncertain.state is LifecycleState.RECOVERY_REQUIRED
    with pytest.raises(LifecycleConflict, match="requires scope_verified"):
        advance(subject, "begin_run", "fixture-run-retry", BASE + timedelta(seconds=8))

    advance(subject, "begin_destroy", "fixture-destroy-intent", BASE + timedelta(seconds=21))
    verified = verify_cleanup(
        subject,
        cleanup_observation(subject, result, observed_at=BASE + timedelta(seconds=22)),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED
    finalized = advance(subject, "finalize", "fixture-finalize", BASE + timedelta(seconds=23))
    assert finalized.final_outcome == "indeterminate"
    assert finalized.success_available is False


def test_ambiguous_destroy_requires_cleanup_observation_before_success() -> None:
    subject = fixture()
    result = provision(subject)
    terminal_with_outputs(subject, result)
    advance(subject, "begin_destroy", "fixture-destroy-intent", BASE + timedelta(seconds=10))
    uncertain = advance(
        subject,
        "observe_destroy_ack",
        "fixture-destroy-ambiguous",
        BASE + timedelta(seconds=11),
        destroy_operation_id=DESTROY_OP,
        status="ambiguous",
    )
    assert uncertain.state is LifecycleState.CLEANUP_UNCERTAIN
    assert uncertain.success_available is False

    verified = verify_cleanup(
        subject,
        cleanup_observation(subject, result, observed_at=BASE + timedelta(seconds=12)),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED
    finalized = advance(subject, "finalize", "fixture-finalize", BASE + timedelta(seconds=13))
    assert finalized.final_outcome == "passed"
    assert finalized.success_available is True


def test_cleanup_observation_cannot_precede_discovered_incarnation() -> None:
    subject = fixture()
    advance(subject, "begin_provision", "fixture-provision-intent", BASE + timedelta(seconds=2))
    advance(
        subject,
        "observe_provision",
        "fixture-provision-ambiguous",
        BASE + timedelta(seconds=3),
        provision_operation_id=PROVISION_OP,
        resource=None,
        status="ambiguous",
    )
    advance(subject, "begin_destroy", "fixture-destroy-intent", BASE + timedelta(seconds=4))
    discovered_resource = resource(provisioned_at=BASE + timedelta(seconds=6))
    early_observation = cleanup_observation(
        subject,
        discovered_resource,
        observed_at=BASE + timedelta(seconds=5),
    )
    rejected = verify_cleanup(subject, early_observation)
    assert rejected.state is LifecycleState.CLEANUP_UNCERTAIN
    assert rejected.success_available is False

    verified = verify_cleanup(
        subject,
        cleanup_observation(
            subject,
            discovered_resource,
            observed_at=BASE + timedelta(seconds=7),
        ),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED


def test_exact_event_replay_is_idempotent_and_conflicting_replay_rejected() -> None:
    subject = fixture()
    first = advance(
        subject, "begin_provision", "stable-provision-intent", BASE + timedelta(seconds=2)
    )
    replay = advance(
        subject, "begin_provision", "stable-provision-intent", BASE + timedelta(seconds=2)
    )
    assert replay == first
    with pytest.raises(LifecycleReplayConflict, match="different content"):
        advance(
            subject,
            "begin_provision",
            "stable-provision-intent",
            BASE + timedelta(seconds=3),
        )


def test_stale_controller_fence_fails_without_mutating_record() -> None:
    subject = fixture()
    before = subject.store.get(ADMISSION)
    subject.store.advance_fence(FENCE + 1)

    with pytest.raises(LifecycleStale, match="controller or fence is stale"):
        advance(subject, "begin_provision", "stale-provision-intent", BASE + timedelta(seconds=2))
    assert subject.store.get(ADMISSION) == before


def test_scope_and_incarnation_mismatch_fail_closed_before_run() -> None:
    subject = fixture()
    result = provision(subject)
    rejected = advance(
        subject,
        "verify_scope",
        "bad-scope-verification",
        BASE + timedelta(seconds=5),
        resource_incarnation_id="different-incarnation",
        observed_scope_digest="sha256-wrong-scope",
    )
    assert rejected.state is LifecycleState.CLEANUP_UNCERTAIN
    assert rejected.success_available is False
    with pytest.raises(LifecycleConflict, match="requires scope_verified"):
        advance(subject, "begin_run", "must-not-run", BASE + timedelta(seconds=6))

    advance(subject, "begin_destroy", "fixture-destroy-intent", BASE + timedelta(seconds=7))
    mismatched = verify_cleanup(
        subject,
        cleanup_observation(
            subject,
            resource(incarnation_id="wrong-incarnation"),
            observed_at=BASE + timedelta(seconds=8),
        ),
    )
    assert mismatched.state is LifecycleState.CLEANUP_UNCERTAIN
    assert mismatched.success_available is False
    mismatched_membership = verify_cleanup(
        subject,
        cleanup_observation(
            subject,
            resource(owned_resource_ids=("fixture-resource-vm",)),
            observed_at=BASE + timedelta(seconds=9),
        ),
    )
    assert mismatched_membership.state is LifecycleState.CLEANUP_UNCERTAIN
    verified = verify_cleanup(
        subject,
        cleanup_observation(subject, result, observed_at=BASE + timedelta(seconds=10)),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED


def test_cleanup_is_late_and_cannot_be_backdated() -> None:
    subject = fixture()
    result = provision(subject)
    terminal_with_outputs(subject, result)
    deadline = subject.lease.execution_deadline
    destroy_at = deadline + timedelta(seconds=10)
    advance(subject, "begin_destroy", "fixture-destroy-intent", destroy_at)

    earlier_than_destroy = cleanup_observation(
        subject,
        result,
        observed_at=deadline + timedelta(seconds=9),
    )
    with pytest.raises(LifecycleConflict, match="cannot be backdated"):
        verify_cleanup(subject, earlier_than_destroy)
    assert subject.store.get(ADMISSION).state is LifecycleState.DESTROY_INTENT

    late = cleanup_observation(subject, result, observed_at=destroy_at + timedelta(seconds=1))
    verified = verify_cleanup(subject, late)
    assert verified.state is LifecycleState.CLEANUP_VERIFIED
    assert verified.events[-1].at > deadline
    finalized = advance(subject, "finalize", "fixture-finalize", destroy_at + timedelta(seconds=2))
    assert finalized.final_outcome == "indeterminate"
    assert finalized.success_available is False


def test_late_workload_terminal_cannot_produce_success() -> None:
    subject = fixture("fixture-late-workload-terminal")
    result = provision(subject)
    running(subject, result)
    terminal_at = subject.lease.execution_deadline + timedelta(seconds=1)
    advance(
        subject,
        "workload_terminal",
        "fixture-workload-terminal",
        terminal_at,
        outcome="passed",
    )
    advance(
        subject,
        "seal_outputs",
        "fixture-output-seal",
        terminal_at + timedelta(seconds=1),
        outputs={"report.json": b"{}", "result.bin": b"abc"},
    )
    advance(
        subject,
        "begin_destroy",
        "fixture-destroy-intent",
        terminal_at + timedelta(seconds=2),
    )
    verified = verify_cleanup(
        subject,
        cleanup_observation(
            subject,
            result,
            observed_at=terminal_at + timedelta(seconds=3),
        ),
    )
    finalized = advance(
        subject,
        "finalize",
        "fixture-finalize",
        terminal_at + timedelta(seconds=4),
    )
    assert verified.state is LifecycleState.CLEANUP_VERIFIED
    assert finalized.final_outcome == "indeterminate"
    assert finalized.success_available is False


def test_output_snapshot_is_exact_bounded_and_detached_from_caller_mapping() -> None:
    subject = fixture()
    result = provision(subject)
    running(subject, result)
    advance(
        subject,
        "workload_terminal",
        "fixture-workload-terminal",
        BASE + timedelta(seconds=8),
        outcome="passed",
    )
    outputs = {"report.json": b"{}", "result.bin": b"abc"}
    sealed = advance(
        subject,
        "seal_outputs",
        "fixture-output-seal",
        BASE + timedelta(seconds=9),
        outputs=outputs,
    )
    assert sealed.output_snapshot is not None
    original_snapshot = sealed.output_snapshot
    outputs["result.bin"] = b"changed"
    assert sealed.output_snapshot == original_snapshot
    assert [(row.path, row.content) for row in original_snapshot.artifacts] == [
        ("report.json", b"{}"),
        ("result.bin", b"abc"),
    ]
    assert original_snapshot.total_bytes == 5

    another = fixture("fixture-admission-overflow")
    overflow_resource = provision(another)
    running(another, overflow_resource)
    advance(
        another,
        "workload_terminal",
        "fixture-workload-terminal",
        BASE + timedelta(seconds=8),
        outcome="passed",
    )
    revision_before = another.store.get(another.lease.admission_id).revision
    with pytest.raises(LifecycleConflict, match="byte limit"):
        advance(
            another,
            "seal_outputs",
            "fixture-output-overflow",
            BASE + timedelta(seconds=9),
            outputs={"report.json": b"{}", "result.bin": b"x" * 40},
        )
    assert another.store.get(another.lease.admission_id).revision == revision_before
    with pytest.raises(LifecycleConflict, match="exactly match"):
        advance(
            another,
            "seal_outputs",
            "fixture-output-path-mismatch",
            BASE + timedelta(seconds=9),
            outputs={"caller/selected/path": b"{}"},
        )
    assert another.store.get(another.lease.admission_id).revision == revision_before


def test_lease_and_workload_reject_invalid_fixture_contracts() -> None:
    with pytest.raises(LifecycleError, match="created by claim time"):
        AuthenticatedLease(
            admission_id="admission",
            controller_id=CONTROLLER,
            fence=FENCE,
            principal_id="principal",
            lease_id="lease",
            created_at=BASE + timedelta(seconds=2),
            claimed_at=BASE + timedelta(seconds=1),
            execution_deadline=BASE + timedelta(seconds=20),
        )
    with pytest.raises(LifecycleError, match="sorted unique tuple"):
        FixedWorkload(
            workload_id="workload",
            workload_digest="digest",
            scope_digest=SCOPE,
            required_output_paths=("report.json", "report.json"),
            expected_cleanup_observer_id=EXPECTED_OBSERVER,
            max_output_bytes=10,
        )


def test_claim_rejects_executor_as_cleanup_observer_without_record_mutation() -> None:
    subject = fixture("fixture-self-observer")
    store = InMemoryLifecycleStore(CONTROLLER, FENCE)
    workload = replace(
        subject.workload,
        expected_cleanup_observer_id=subject.lease.principal_id,
    )

    with pytest.raises(LifecycleError, match="distinct from executor principal"):
        store.claim(
            subject.lease,
            workload,
            controller_id=CONTROLLER,
            fence=FENCE,
            operation_id="fixture-self-observer-claim",
        )
    with pytest.raises(LifecycleConflict, match="not been claimed"):
        store.get(subject.lease.admission_id)
