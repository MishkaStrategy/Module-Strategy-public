"""Test-only reference state machine for disposable executor lifecycle.

This module is deliberately isolated from runtime packages. It models intent-before-effect
transitions with an in-memory lock, compare-and-swap revision checks, and an append-only event
tuple. It is neither crash-durable nor cross-process exclusion, and does not provision, execute,
authenticate, publish, or destroy real resources. Its fixed workload and output limits are only
synthetic fixture inputs, not production policy.

The authenticated principal and lease predate claim; the resource incarnation is created only
after claim. This preserves receipt-v1's ``created_at <= claimed_at`` meaning without treating a
pre-claim lease as a provisioned executor. A passing workload remains provisional until an
independent observation verifies cleanup of the exact incarnation and owned resource set.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Any, Callable, Literal, Mapping


class LifecycleError(ValueError):
    """A reference lifecycle transition or immutable input is invalid."""


class LifecycleConflict(LifecycleError):
    """A transition conflicts with the current lifecycle or an earlier event."""


class LifecycleStale(LifecycleConflict):
    """A transition used a stale fencing token or aggregate revision."""


class LifecycleReplayConflict(LifecycleConflict):
    """An operation ID was replayed with different event content."""


class LifecycleState(StrEnum):
    CLAIMED = "claimed"
    PROVISION_INTENT = "provision_intent"
    PROVISIONED = "provisioned"
    SCOPE_VERIFIED = "scope_verified"
    RUN_INTENT = "run_intent"
    RUNNING = "running"
    WORKLOAD_TERMINAL = "workload_terminal"
    OUTPUTS_SEALED = "outputs_sealed"
    DESTROY_INTENT = "destroy_intent"
    CLEANUP_VERIFIED = "cleanup_verified"
    RECOVERY_REQUIRED = "recovery_required"
    CLEANUP_UNCERTAIN = "cleanup_uncertain"
    FINALIZED = "finalized"


@dataclass(frozen=True, slots=True)
class AuthenticatedLease:
    """Synthetic pre-claim principal/lease binding, distinct from a resource incarnation."""

    admission_id: str
    controller_id: str
    fence: int
    principal_id: str
    lease_id: str
    created_at: datetime
    claimed_at: datetime
    execution_deadline: datetime

    def __post_init__(self) -> None:
        _require_ids(
            self.admission_id,
            self.controller_id,
            self.principal_id,
            self.lease_id,
        )
        if not _is_int(self.fence) or self.fence < 1:
            raise LifecycleError("fence must be a positive integer")
        _require_aware(self.created_at, self.claimed_at, self.execution_deadline)
        if not self.created_at <= self.claimed_at < self.execution_deadline:
            raise LifecycleError("lease must be created by claim time and expire after claim")


@dataclass(frozen=True, slots=True)
class FixedWorkload:
    """Synthetic fixed workload, output contract, and expected observer ID; no command is accepted.

    The observer ID is only a fixture binding. Matching it does not authenticate an observer.
    """

    workload_id: str
    workload_digest: str
    scope_digest: str
    required_output_paths: tuple[str, ...]
    expected_cleanup_observer_id: str
    max_output_bytes: int

    def __post_init__(self) -> None:
        _require_ids(
            self.workload_id,
            self.workload_digest,
            self.scope_digest,
            self.expected_cleanup_observer_id,
        )
        if (
            not isinstance(self.required_output_paths, tuple)
            or not self.required_output_paths
            or any(not _safe_relative_path(path) for path in self.required_output_paths)
            or tuple(sorted(set(self.required_output_paths))) != self.required_output_paths
        ):
            raise LifecycleError("output paths must be a non-empty sorted unique tuple")
        if not _is_int(self.max_output_bytes) or self.max_output_bytes <= 0:
            raise LifecycleError("synthetic output byte limit must be positive")


@dataclass(frozen=True, slots=True)
class ResourceIncarnation:
    """A post-claim provision result tied to one stable provision operation."""

    resource_id: str
    incarnation_id: str
    provision_operation_id: str
    scope_digest: str
    owned_resource_ids: tuple[str, ...]
    provisioned_at: datetime

    def __post_init__(self) -> None:
        _require_ids(
            self.resource_id,
            self.incarnation_id,
            self.provision_operation_id,
            self.scope_digest,
        )
        if (
            not isinstance(self.owned_resource_ids, tuple)
            or not self.owned_resource_ids
            or any(not isinstance(value, str) or not value for value in self.owned_resource_ids)
            or tuple(sorted(set(self.owned_resource_ids))) != self.owned_resource_ids
        ):
            raise LifecycleError("owned resource IDs must be a non-empty sorted unique tuple")
        _require_aware(self.provisioned_at)


@dataclass(frozen=True, slots=True)
class OutputArtifact:
    path: str
    content: bytes
    sha256: str

    def __post_init__(self) -> None:
        if not _safe_relative_path(self.path) or not isinstance(self.content, bytes):
            raise LifecycleError("output artifact requires a safe path and immutable bytes")
        if self.sha256 != hashlib.sha256(self.content).hexdigest():
            raise LifecycleError("output artifact digest does not match its bytes")


@dataclass(frozen=True, slots=True)
class OutputSnapshot:
    artifacts: tuple[OutputArtifact, ...]
    total_bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CleanupObservation:
    """Synthetic independent inventory observation, not an authenticated attestation."""

    observer_id: str
    provision_operation_id: str
    scope_digest: str
    resource: ResourceIncarnation | None
    remaining_resource_ids: tuple[str, ...]
    observed_at: datetime

    def __post_init__(self) -> None:
        _require_ids(self.observer_id, self.provision_operation_id, self.scope_digest)
        if (
            not isinstance(self.remaining_resource_ids, tuple)
            or any(not isinstance(value, str) or not value for value in self.remaining_resource_ids)
            or tuple(sorted(set(self.remaining_resource_ids))) != self.remaining_resource_ids
        ):
            raise LifecycleError("remaining resource IDs must be a sorted unique tuple")
        _require_aware(self.observed_at)


@dataclass(frozen=True, slots=True)
class LifecycleEvent:
    operation_id: str
    kind: str
    payload_fingerprint: str
    at: datetime
    revision: int


@dataclass(frozen=True, slots=True)
class LifecycleRecord:
    lease: AuthenticatedLease
    workload: FixedWorkload
    state: LifecycleState
    revision: int
    events: tuple[LifecycleEvent, ...]
    provision_operation_id: str | None = None
    provision_intent_at: datetime | None = None
    resource: ResourceIncarnation | None = None
    run_operation_id: str | None = None
    run_acknowledged: bool = False
    workload_outcome: Literal["passed", "failed"] | None = None
    output_snapshot: OutputSnapshot | None = None
    destroy_operation_id: str | None = None
    destroy_acknowledged: bool = False
    cleanup_observation: CleanupObservation | None = None
    recovery_reason: str | None = None
    final_outcome: Literal["passed", "failed", "indeterminate"] | None = None

    @property
    def success_available(self) -> bool:
        return self.state is LifecycleState.FINALIZED and self.final_outcome == "passed"

    @property
    def external_trust_established(self) -> bool:
        return False

    @property
    def acceptance_eligible(self) -> bool:
        return False


Transition = Callable[[LifecycleRecord], LifecycleRecord]


class InMemoryLifecycleStore:
    """Single-process reference reducer; lock/CAS/event tuples are not durable or distributed."""

    def __init__(self, controller_id: str, current_fence: int) -> None:
        _require_ids(controller_id)
        if not _is_int(current_fence) or current_fence < 1:
            raise LifecycleError("current fence must be a positive integer")
        self._controller_id = controller_id
        self._current_fence = current_fence
        self._lock = threading.RLock()
        self._records: dict[str, LifecycleRecord] = {}
        self._operations: dict[tuple[str, str], tuple[str, str]] = {}

    def advance_fence(self, new_fence: int) -> None:
        """Advance the synthetic controller fence for stale-token regression tests."""
        with self._lock:
            if not _is_int(new_fence) or new_fence <= self._current_fence:
                raise LifecycleStale("new controller fence must strictly advance")
            self._current_fence = new_fence

    def claim(
        self,
        lease: AuthenticatedLease,
        workload: FixedWorkload,
        *,
        controller_id: str,
        fence: int,
        operation_id: str,
    ) -> LifecycleRecord:
        _require_ids(operation_id)
        with self._lock:
            self._authorize(controller_id, fence)
            if lease.controller_id != controller_id or lease.fence != fence:
                raise LifecycleStale("lease controller or fence does not match current authority")
            if workload.expected_cleanup_observer_id == lease.principal_id:
                raise LifecycleError("cleanup observer must be distinct from executor principal")
            key = (lease.admission_id, operation_id)
            fingerprint = _fingerprint(("claim", lease, workload, lease.claimed_at))
            prior = self._operations.get(key)
            existing = self._records.get(lease.admission_id)
            if prior is not None:
                if prior[0] != "claim" or prior[1] != fingerprint:
                    raise LifecycleReplayConflict(
                        "claim operation ID replayed with different content"
                    )
                if existing is None:
                    raise LifecycleConflict("claim replay has no lifecycle record")
                return existing
            if existing is not None:
                raise LifecycleConflict("admission already has a lifecycle record")
            event = LifecycleEvent(operation_id, "claim", fingerprint, lease.claimed_at, 1)
            record = LifecycleRecord(
                lease=lease,
                workload=workload,
                state=LifecycleState.CLAIMED,
                revision=1,
                events=(event,),
            )
            self._records[lease.admission_id] = record
            self._operations[key] = ("claim", fingerprint)
            return record

    def get(self, admission_id: str) -> LifecycleRecord:
        with self._lock:
            try:
                return self._records[admission_id]
            except KeyError as exc:
                raise LifecycleConflict("admission has not been claimed") from exc

    def begin_provision(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        now: datetime,
    ) -> LifecycleRecord:
        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="provision_intent",
            payload=(),
            now=now,
            mutate=lambda row: (
                _replace_state(
                    row,
                    LifecycleState.PROVISION_INTENT,
                    provision_operation_id=operation_id,
                    provision_intent_at=now,
                )
                if row.state is LifecycleState.CLAIMED
                else _reject("provision intent requires claimed state")
            ),
        )

    def observe_provision(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        provision_operation_id: str,
        resource: ResourceIncarnation | None,
        status: Literal["acknowledged", "ambiguous", "failed"],
        now: datetime,
    ) -> LifecycleRecord:
        payload = (provision_operation_id, resource, status)

        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            _require_state(row, LifecycleState.PROVISION_INTENT)
            if provision_operation_id != row.provision_operation_id:
                return _replace_state(
                    row,
                    LifecycleState.RECOVERY_REQUIRED,
                    resource=resource,
                    recovery_reason="provision operation binding mismatch",
                )
            if resource is not None and (
                resource.provision_operation_id != provision_operation_id
                or row.provision_intent_at is None
                or resource.provisioned_at < row.provision_intent_at
                or resource.provisioned_at > now
                or resource.provisioned_at < row.lease.claimed_at
            ):
                return _replace_state(
                    row,
                    LifecycleState.RECOVERY_REQUIRED,
                    resource=resource,
                    recovery_reason="provision resource time or operation binding is invalid",
                )
            if status != "acknowledged" or resource is None:
                return _replace_state(
                    row,
                    LifecycleState.RECOVERY_REQUIRED,
                    resource=resource,
                    recovery_reason=f"provision outcome {status} requires recovery",
                )
            return _replace_state(row, LifecycleState.PROVISIONED, resource=resource)

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="provision_observation",
            payload=payload,
            now=now,
            mutate=mutate,
        )

    def verify_scope(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        resource_incarnation_id: str,
        observed_scope_digest: str,
        now: datetime,
    ) -> LifecycleRecord:
        payload = (resource_incarnation_id, observed_scope_digest)

        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            _require_state(row, LifecycleState.PROVISIONED)
            if row.resource is None:
                return _replace_state(
                    row,
                    LifecycleState.CLEANUP_UNCERTAIN,
                    recovery_reason="provisioned state lacks resource incarnation",
                )
            if (
                resource_incarnation_id != row.resource.incarnation_id
                or observed_scope_digest != row.workload.scope_digest
                or row.resource.scope_digest != row.workload.scope_digest
            ):
                return _replace_state(
                    row,
                    LifecycleState.CLEANUP_UNCERTAIN,
                    recovery_reason="resource incarnation or execution scope mismatch",
                )
            return _replace_state(row, LifecycleState.SCOPE_VERIFIED)

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="scope_verification",
            payload=payload,
            now=now,
            mutate=mutate,
        )

    def begin_run(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        now: datetime,
    ) -> LifecycleRecord:
        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            _require_state(row, LifecycleState.SCOPE_VERIFIED)
            if now > row.lease.execution_deadline:
                raise LifecycleConflict("execution deadline has passed")
            return _replace_state(row, LifecycleState.RUN_INTENT, run_operation_id=operation_id)

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="run_intent",
            payload=(),
            now=now,
            mutate=mutate,
        )

    def observe_run(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        run_operation_id: str,
        status: Literal["acknowledged", "ambiguous", "failed"],
        now: datetime,
    ) -> LifecycleRecord:
        payload = (run_operation_id, status)

        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            _require_state(row, LifecycleState.RUN_INTENT)
            if run_operation_id != row.run_operation_id:
                return _replace_state(
                    row,
                    LifecycleState.RECOVERY_REQUIRED,
                    recovery_reason="run operation binding mismatch; rerun is prohibited",
                )
            if status != "acknowledged":
                return _replace_state(
                    row,
                    LifecycleState.RECOVERY_REQUIRED,
                    recovery_reason=f"run outcome {status}; rerun is prohibited",
                )
            return _replace_state(row, LifecycleState.RUNNING, run_acknowledged=True)

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="run_observation",
            payload=payload,
            now=now,
            mutate=mutate,
        )

    def workload_terminal(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        outcome: Literal["passed", "failed"],
        now: datetime,
    ) -> LifecycleRecord:
        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="workload_terminal",
            payload=(outcome,),
            now=now,
            mutate=lambda row: (
                _replace_state(
                    row,
                    LifecycleState.WORKLOAD_TERMINAL,
                    workload_outcome=outcome,
                )
                if row.state is LifecycleState.RUNNING
                else _reject("workload terminal observation requires running state")
            ),
        )

    def seal_outputs(
        self,
        admission_id: str,
        outputs: Mapping[str, bytes],
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        now: datetime,
    ) -> LifecycleRecord:
        snapshot = _seal_snapshot(self.get(admission_id).workload, outputs)
        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="outputs_sealed",
            payload=(snapshot,),
            now=now,
            mutate=lambda row: (
                _replace_state(
                    row,
                    LifecycleState.OUTPUTS_SEALED,
                    output_snapshot=snapshot,
                )
                if row.state is LifecycleState.WORKLOAD_TERMINAL
                else _reject("outputs can only be sealed after terminal workload outcome")
            ),
        )

    def begin_destroy(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        now: datetime,
    ) -> LifecycleRecord:
        allowed = {
            LifecycleState.PROVISION_INTENT,
            LifecycleState.PROVISIONED,
            LifecycleState.SCOPE_VERIFIED,
            LifecycleState.RUN_INTENT,
            LifecycleState.RUNNING,
            LifecycleState.WORKLOAD_TERMINAL,
            LifecycleState.OUTPUTS_SEALED,
            LifecycleState.RECOVERY_REQUIRED,
            LifecycleState.CLEANUP_UNCERTAIN,
        }

        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            if row.state not in allowed:
                raise LifecycleConflict("destroy intent is invalid from current state")
            return _replace_state(
                row,
                LifecycleState.DESTROY_INTENT,
                destroy_operation_id=operation_id,
                destroy_acknowledged=False,
            )

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="destroy_intent",
            payload=(),
            now=now,
            mutate=mutate,
        )

    def observe_destroy_ack(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        destroy_operation_id: str,
        status: Literal["acknowledged", "ambiguous", "failed"],
        now: datetime,
    ) -> LifecycleRecord:
        payload = (destroy_operation_id, status)

        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            _require_state(row, LifecycleState.DESTROY_INTENT)
            if destroy_operation_id != row.destroy_operation_id:
                return _replace_state(
                    row,
                    LifecycleState.CLEANUP_UNCERTAIN,
                    recovery_reason="destroy operation binding mismatch",
                )
            if status == "acknowledged":
                # ACK records provider response but is deliberately not cleanup proof.
                return replace(row, destroy_acknowledged=True)
            return _replace_state(
                row,
                LifecycleState.CLEANUP_UNCERTAIN,
                recovery_reason=f"destroy outcome {status} requires independent cleanup proof",
            )

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="destroy_observation",
            payload=payload,
            now=now,
            mutate=mutate,
        )

    def verify_cleanup(
        self,
        admission_id: str,
        observation: CleanupObservation,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
    ) -> LifecycleRecord:
        payload = (observation,)

        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            if row.state not in {
                LifecycleState.DESTROY_INTENT,
                LifecycleState.CLEANUP_UNCERTAIN,
            }:
                raise LifecycleConflict("cleanup verification requires destroy or uncertain state")
            resource_matches = (
                observation.resource == row.resource
                if row.resource is not None
                else observation.resource is None
                or observation.resource.provision_operation_id == row.provision_operation_id
            )
            valid = (
                observation.observer_id == row.workload.expected_cleanup_observer_id
                and observation.observer_id != row.lease.principal_id
                and observation.provision_operation_id == row.provision_operation_id
                and observation.scope_digest == row.workload.scope_digest
                and observation.remaining_resource_ids == ()
                and resource_matches
                and (
                    observation.resource is None
                    or (
                        observation.resource.scope_digest == row.workload.scope_digest
                        and observation.resource.provision_operation_id
                        == row.provision_operation_id
                        and observation.resource.provisioned_at >= row.lease.claimed_at
                        and observation.resource.provisioned_at <= observation.observed_at
                    )
                )
            )
            if not valid:
                return _replace_state(
                    row,
                    LifecycleState.CLEANUP_UNCERTAIN,
                    cleanup_observation=observation,
                    recovery_reason="independent cleanup observation does not match exact owned scope",
                )
            return _replace_state(
                row,
                LifecycleState.CLEANUP_VERIFIED,
                resource=row.resource or observation.resource,
                cleanup_observation=observation,
                recovery_reason=None,
            )

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="cleanup_verification",
            payload=payload,
            now=observation.observed_at,
            mutate=mutate,
        )

    def finalize(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        now: datetime,
    ) -> LifecycleRecord:
        def mutate(row: LifecycleRecord) -> LifecycleRecord:
            _require_state(row, LifecycleState.CLEANUP_VERIFIED)
            terminal_at = next(
                (event.at for event in row.events if event.kind == "workload_terminal"), None
            )
            cleanup_at = (
                row.cleanup_observation.observed_at if row.cleanup_observation is not None else None
            )
            if (
                row.workload_outcome == "passed"
                and row.output_snapshot is not None
                and terminal_at is not None
                and cleanup_at is not None
                and terminal_at <= cleanup_at <= row.lease.execution_deadline
            ):
                outcome: Literal["passed", "failed", "indeterminate"] = "passed"
            elif row.workload_outcome == "failed":
                outcome = "failed"
            else:
                outcome = "indeterminate"
            return _replace_state(
                row,
                LifecycleState.FINALIZED,
                final_outcome=outcome,
            )

        return self._apply(
            admission_id,
            controller_id=controller_id,
            fence=fence,
            expected_revision=expected_revision,
            operation_id=operation_id,
            kind="finalize",
            payload=(),
            now=now,
            mutate=mutate,
        )

    def _apply(
        self,
        admission_id: str,
        *,
        controller_id: str,
        fence: int,
        expected_revision: int,
        operation_id: str,
        kind: str,
        payload: object,
        now: datetime,
        mutate: Transition,
    ) -> LifecycleRecord:
        _require_ids(admission_id, operation_id)
        _require_aware(now)
        with self._lock:
            self._authorize(controller_id, fence)
            try:
                current = self._records[admission_id]
            except KeyError as exc:
                raise LifecycleConflict("admission has not been claimed") from exc
            if current.lease.controller_id != controller_id or current.lease.fence != fence:
                raise LifecycleStale("transition lease does not match controller fence")
            key = (admission_id, operation_id)
            fingerprint = _fingerprint((kind, payload, now))
            prior = self._operations.get(key)
            if prior is not None:
                if prior != (kind, fingerprint):
                    raise LifecycleReplayConflict("operation ID replayed with different content")
                return current
            if expected_revision != current.revision:
                raise LifecycleStale(
                    f"expected revision {expected_revision}, current revision {current.revision}"
                )
            if now < current.events[-1].at:
                raise LifecycleConflict("event time cannot be backdated")
            updated = mutate(current)
            event = LifecycleEvent(
                operation_id=operation_id,
                kind=kind,
                payload_fingerprint=fingerprint,
                at=now,
                revision=current.revision + 1,
            )
            updated = replace(
                updated,
                revision=current.revision + 1,
                events=current.events + (event,),
            )
            self._records[admission_id] = updated
            self._operations[key] = (kind, fingerprint)
            return updated

    def _authorize(self, controller_id: str, fence: int) -> None:
        if controller_id != self._controller_id or fence != self._current_fence:
            raise LifecycleStale("controller or fence is stale")


def _seal_snapshot(workload: FixedWorkload, outputs: Mapping[str, bytes]) -> OutputSnapshot:
    if set(outputs) != set(workload.required_output_paths):
        raise LifecycleConflict("output paths must exactly match the fixed workload contract")
    if any(not isinstance(content, bytes) for content in outputs.values()):
        raise LifecycleConflict("output contents must be immutable bytes")
    copied = tuple(
        OutputArtifact(path, bytes(outputs[path]), hashlib.sha256(outputs[path]).hexdigest())
        for path in workload.required_output_paths
    )
    total_bytes = sum(len(artifact.content) for artifact in copied)
    if total_bytes > workload.max_output_bytes:
        raise LifecycleConflict("sealed outputs exceed the synthetic byte limit")
    fingerprint_rows = tuple((item.path, item.sha256, len(item.content)) for item in copied)
    snapshot_digest = hashlib.sha256(
        json.dumps(fingerprint_rows, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return OutputSnapshot(copied, total_bytes, snapshot_digest)


def _replace_state(
    record: LifecycleRecord,
    state: LifecycleState,
    **changes: Any,
) -> LifecycleRecord:
    return replace(record, state=state, **changes)


def _require_state(record: LifecycleRecord, expected: LifecycleState) -> None:
    if record.state is not expected:
        raise LifecycleConflict(
            f"transition requires {expected.value}; current state is {record.state.value}"
        )


def _reject(message: str) -> LifecycleRecord:
    raise LifecycleConflict(message)


def _fingerprint(value: object) -> str:
    return hashlib.sha256(repr(value).encode("utf-8")).hexdigest()


def _require_ids(*values: str) -> None:
    if any(not isinstance(value, str) or not value for value in values):
        raise LifecycleError("identifiers and digests must be non-empty strings")


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _require_aware(*values: datetime) -> None:
    if any(value.tzinfo is None or value.utcoffset() is None for value in values):
        raise LifecycleError("lifecycle timestamps must be timezone-aware")


def _safe_relative_path(value: object) -> bool:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return False
    path = PurePosixPath(value)
    return not path.is_absolute() and ".." not in path.parts and path.as_posix() == value
