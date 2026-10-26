from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Protocol

from .contracts import (
    AcceptanceErrorCode,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceMutationResult,
    AcceptanceOwnerError,
    AcceptancePhase,
    AcceptanceRunSnapshot,
    AcceptanceStatus,
    EvidenceReference,
    PhaseOutcome,
    PhaseOutcomeStatus,
    ResourceSample,
    TERMINAL_STATUSES,
)
from .storage import SQLiteAcceptanceStore, StoredRun

_LEASE_SECONDS = 60
_MAX_ACTIVE_INCREMENT = 300.0
_MAX_RESOURCES = 120
_MAX_WARNINGS = 32
_MAX_EVIDENCE = 64


@dataclass(frozen=True, slots=True)
class PhaseExecutionContext:
    acceptance_run_id: str
    phase: AcceptancePhase
    attempt_id: str
    recovering: bool
    full_rc_sha: str
    dataset_id: str
    dataset_digest: str
    strategy_id: str
    strategy_digest: str
    config_digest: str
    active_test_time: float
    checkpoint_token: str | None
    checkpoint_digest: str | None


class AcceptancePhaseExecutor(Protocol):
    """One bounded owner phase slice. Implementations are allowlisted by composition."""

    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome: ...

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome: ...


class AcceptanceExecutorRegistry:
    def __init__(self, executors: Mapping[AcceptancePhase, AcceptancePhaseExecutor]) -> None:
        if not isinstance(executors, Mapping):
            raise TypeError("executors must be a mapping")
        normalized: dict[AcceptancePhase, AcceptancePhaseExecutor] = {}
        for phase, executor in executors.items():
            if not isinstance(phase, AcceptancePhase):
                raise TypeError("executor key must be AcceptancePhase")
            if not callable(getattr(executor, "execute", None)) or not callable(
                getattr(executor, "recover", None)
            ):
                raise TypeError("executor must provide execute() and recover()")
            normalized[phase] = executor
        self._executors = normalized

    def for_phase(self, phase: AcceptancePhase) -> AcceptancePhaseExecutor:
        try:
            return self._executors[phase]
        except KeyError:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.PHASE_BLOCKED,
                "acceptance phase executor is not configured",
            ) from None


class SystemAcceptanceOwner:
    """Application-level durable acceptance/soak owner."""

    def __init__(
        self,
        store: SQLiteAcceptanceStore,
        executors: AcceptanceExecutorRegistry,
        *,
        instance_id: str,
        clock: Callable[[], datetime] | None = None,
        lease_seconds: int = _LEASE_SECONDS,
    ) -> None:
        if not isinstance(store, SQLiteAcceptanceStore):
            raise TypeError("store must be SQLiteAcceptanceStore")
        if not isinstance(executors, AcceptanceExecutorRegistry):
            raise TypeError("executors must be AcceptanceExecutorRegistry")
        if not isinstance(instance_id, str) or not _safe_identifier(instance_id):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "acceptance owner instance_id is invalid",
            )
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "lease_seconds must be an integer",
            )
        if not 5 <= lease_seconds <= 300:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "lease_seconds must be between 5 and 300",
            )
        self.store = store
        self.executors = executors
        self.instance_id = instance_id
        self.clock = clock or (lambda: datetime.now(UTC))
        self.lease_seconds = lease_seconds

    def mutate(self, request: AcceptanceMutationRequest) -> AcceptanceMutationResult:
        if not isinstance(request, AcceptanceMutationRequest):
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "request must be AcceptanceMutationRequest",
            )
        now = self._now()
        fingerprint = _fingerprint(request)
        pending_error: AcceptanceOwnerError | None = None
        completed_result: AcceptanceMutationResult | None = None
        with self.store.transaction() as db:
            existing = self.store.mutation_row(request.request_id, connection=db)
            if existing is not None:
                completed_result, pending_error = self._inspect_existing_mutation(
                    request,
                    fingerprint,
                    existing,
                )
                audit_run_id, audit_status, audit_phase = self._audit_context(
                    request,
                    completed_result,
                    pending_error,
                    db,
                )
                self.store.append_audit(
                    timestamp=now,
                    mutation=request.mutation.value,
                    request_id=request.request_id,
                    operator=request.operator.subject,
                    run_id=audit_run_id,
                    result=(
                        "REPLAYED"
                        if completed_result is not None
                        else pending_error.code.value
                        if pending_error is not None
                        else AcceptanceErrorCode.RECOVERY_REQUIRED.value
                    ),
                    previous_status=audit_status.value if audit_status is not None else None,
                    new_status=audit_status.value if audit_status is not None else None,
                    phase=audit_phase.value,
                    connection=db,
                )
            else:
                self.store.insert_mutation(
                    request_id=request.request_id,
                    fingerprint=fingerprint,
                    created_at=now,
                    connection=db,
                )
                previous_status: AcceptanceStatus | None = None
                try:
                    completed_result, previous_status = self._apply_mutation(request, now, db)
                except AcceptanceOwnerError as exc:
                    pending_error = AcceptanceOwnerError(
                        exc.code,
                        str(exc),
                        request_id=request.request_id,
                        run_id=exc.run_id or request.run_id,
                    )
                    audit_run_id, audit_status, audit_phase = self._audit_context(
                        request,
                        None,
                        pending_error,
                        db,
                    )
                    self.store.finish_mutation(
                        request_id=request.request_id,
                        result=None,
                        error_code=exc.code,
                        completed_at=now,
                        connection=db,
                    )
                    self.store.append_audit(
                        timestamp=now,
                        mutation=request.mutation.value,
                        request_id=request.request_id,
                        operator=request.operator.subject,
                        run_id=audit_run_id,
                        result=exc.code.value,
                        previous_status=(
                            previous_status.value if previous_status is not None else None
                        ),
                        new_status=audit_status.value if audit_status is not None else None,
                        phase=audit_phase.value,
                        connection=db,
                    )
                else:
                    assert completed_result is not None
                    self.store.finish_mutation(
                        request_id=request.request_id,
                        result=_mutation_result_mapping(completed_result),
                        error_code=None,
                        completed_at=now,
                        connection=db,
                    )
                    self.store.append_audit(
                        timestamp=now,
                        mutation=request.mutation.value,
                        request_id=request.request_id,
                        operator=request.operator.subject,
                        run_id=completed_result.acceptance_run_id,
                        result="SUCCEEDED",
                        previous_status=(
                            previous_status.value if previous_status is not None else None
                        ),
                        new_status=completed_result.status.value,
                        phase=completed_result.phase.value,
                        connection=db,
                    )
        if pending_error is not None:
            raise pending_error from None
        if completed_result is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "acceptance mutation completed without a durable result",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        return completed_result

    def snapshot(self, run_id: str | None = None) -> AcceptanceRunSnapshot | None:
        stored = self.store.load_run(run_id) if run_id is not None else self.store.latest_run()
        if stored is None:
            return None
        return self._with_wall_clock(stored.snapshot)

    def recover_after_restart(self) -> AcceptanceRunSnapshot | None:
        now = self._now()
        with self.store.transaction() as db:
            stored = self.store.active_run(connection=db)
            if stored is None:
                return None
            if stored.phase_execution == "IN_PROGRESS":
                if stored.lease_expires_at is None:
                    raise AcceptanceOwnerError(
                        AcceptanceErrorCode.RECOVERY_REQUIRED,
                        "active acceptance phase lease is incomplete",
                        run_id=stored.snapshot.acceptance_run_id,
                    )
                if stored.lease_expires_at > now:
                    return self._with_wall_clock(stored.snapshot)
            snapshot = replace(
                stored.snapshot,
                status=AcceptanceStatus.RECOVERING,
                updated_at=now,
                restart_count=stored.snapshot.restart_count + 1,
                next_action="RECOVER_PHASE",
            )
            recovering_phase = stored.phase_execution == "IN_PROGRESS"
            recovered = replace(
                stored,
                snapshot=snapshot,
                lease_owner=self.instance_id if recovering_phase else None,
                lease_expires_at=now if recovering_phase else None,
            )
            self.store.save_run(recovered, connection=db)
            return self._with_wall_clock(snapshot)

    def step(self) -> AcceptanceRunSnapshot | None:
        """Execute or recover one bounded phase slice."""
        now = self._now()
        with self.store.transaction() as db:
            stored = self.store.active_run(connection=db)
            if stored is None:
                latest = self.store.latest_run()
                return self._with_wall_clock(latest.snapshot) if latest is not None else None
            stored = self._terminalize_requested_stop(stored, now, db)
            if stored.snapshot.status in TERMINAL_STATUSES:
                return self._with_wall_clock(stored.snapshot)

            recovering = stored.phase_execution == "IN_PROGRESS"
            if recovering:
                if stored.lease_expires_at is None or stored.phase_attempt_id is None:
                    raise AcceptanceOwnerError(
                        AcceptanceErrorCode.RECOVERY_REQUIRED,
                        "acceptance phase recovery metadata is incomplete",
                        run_id=stored.snapshot.acceptance_run_id,
                    )
                if stored.lease_expires_at > now and stored.lease_owner != self.instance_id:
                    return self._with_wall_clock(stored.snapshot)
                attempt_id = stored.phase_attempt_id
                snapshot = replace(
                    stored.snapshot,
                    status=AcceptanceStatus.RECOVERING,
                    updated_at=now,
                    next_action="RECOVER_PHASE",
                )
                leased = replace(
                    stored,
                    snapshot=snapshot,
                    lease_owner=self.instance_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                )
            else:
                attempt_number = stored.phase_attempt + 1
                attempt_id = _attempt_id(
                    stored.snapshot.acceptance_run_id,
                    stored.snapshot.phase,
                    attempt_number,
                )
                snapshot = replace(
                    stored.snapshot,
                    status=AcceptanceStatus.RUNNING,
                    started_at=stored.snapshot.started_at or now,
                    updated_at=now,
                    next_action="EXECUTE_PHASE",
                )
                leased = replace(
                    stored,
                    snapshot=snapshot,
                    phase_attempt=attempt_number,
                    phase_attempt_id=attempt_id,
                    phase_execution="IN_PROGRESS",
                    lease_owner=self.instance_id,
                    lease_expires_at=now + timedelta(seconds=self.lease_seconds),
                )
            self.store.save_run(leased, connection=db)
            context = _phase_context(leased, recovering=recovering)

        try:
            executor = self.executors.for_phase(context.phase)
            outcome = executor.recover(context) if recovering else executor.execute(context)
            if not isinstance(outcome, PhaseOutcome):
                raise TypeError("phase executor returned invalid outcome")
        except AcceptanceOwnerError as exc:
            outcome = PhaseOutcome(
                status=(
                    PhaseOutcomeStatus.BLOCKED
                    if exc.code is AcceptanceErrorCode.PHASE_BLOCKED
                    else PhaseOutcomeStatus.FAILED
                ),
                error_code=exc.code.value,
                next_action="OWNER_REVIEW",
            )
        except Exception:
            outcome = PhaseOutcome(
                status=PhaseOutcomeStatus.FAILED,
                error_code=AcceptanceErrorCode.OWNER_FAILURE.value,
                next_action="OWNER_REVIEW",
            )
        return self._complete_phase_slice(context.attempt_id, outcome)

    def _apply_mutation(
        self,
        request: AcceptanceMutationRequest,
        now: datetime,
        db: sqlite3.Connection,
    ) -> tuple[AcceptanceMutationResult, AcceptanceStatus | None]:
        if request.mutation is AcceptanceMutation.START:
            assert request.spec is not None
            active = self.store.active_run(connection=db)
            if active is not None:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.RUN_ALREADY_ACTIVE,
                    "an acceptance run is already active",
                    run_id=active.snapshot.acceptance_run_id,
                )
            run_id = _run_id(request.request_id, _fingerprint(request))
            snapshot = AcceptanceRunSnapshot(
                acceptance_run_id=run_id,
                spec=request.spec,
                status=AcceptanceStatus.QUEUED,
                phase=request.spec.phases[0],
                created_at=now,
                started_at=None,
                updated_at=now,
                completed_at=None,
                active_test_time=0.0,
                wall_clock_elapsed=0.0,
                restart_count=0,
                checkpoint=None,
                resources=(),
                warnings=(),
                error=None,
                next_action="START_WORKER",
                evidence=(),
            )
            created = StoredRun(
                snapshot=snapshot,
                phase_attempt=0,
                phase_attempt_id=None,
                phase_execution="IDLE",
                lease_owner=None,
                lease_expires_at=None,
                stop_requested=False,
                cancel_requested=False,
            )
            self.store.save_run(created, connection=db)
            return _mutation_result(request, snapshot, now), None

        assert request.run_id is not None
        stored = self.store.load_run(request.run_id, connection=db)
        if stored is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.RUN_NOT_FOUND,
                "acceptance run was not found",
                run_id=request.run_id,
            )
        previous = stored.snapshot.status
        if request.mutation is AcceptanceMutation.CANCEL:
            if previous in TERMINAL_STATUSES:
                return _mutation_result(request, stored.snapshot, now), previous
            snapshot = replace(
                stored.snapshot,
                status=(
                    AcceptanceStatus.STOPPING
                    if stored.phase_execution == "IN_PROGRESS"
                    else AcceptanceStatus.CANCELLED
                ),
                updated_at=now,
                completed_at=(None if stored.phase_execution == "IN_PROGRESS" else now),
                next_action=(
                    "CANCEL_AT_PHASE_BOUNDARY" if stored.phase_execution == "IN_PROGRESS" else None
                ),
            )
            updated = replace(stored, snapshot=snapshot, cancel_requested=True)
        elif request.mutation is AcceptanceMutation.STOP:
            if previous in TERMINAL_STATUSES:
                return _mutation_result(request, stored.snapshot, now), previous
            snapshot = replace(
                stored.snapshot,
                status=(
                    AcceptanceStatus.STOPPING
                    if stored.phase_execution == "IN_PROGRESS"
                    else AcceptanceStatus.CANCELLED
                ),
                updated_at=now,
                completed_at=(None if stored.phase_execution == "IN_PROGRESS" else now),
                next_action=(
                    "STOP_AT_PHASE_BOUNDARY" if stored.phase_execution == "IN_PROGRESS" else None
                ),
            )
            updated = replace(stored, snapshot=snapshot, stop_requested=True)
        elif request.mutation is AcceptanceMutation.RETRY:
            if previous not in {AcceptanceStatus.FAILED, AcceptanceStatus.BLOCKED}:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_TRANSITION,
                    "acceptance run is not retryable",
                    run_id=request.run_id,
                )
            snapshot = replace(
                stored.snapshot,
                status=AcceptanceStatus.QUEUED,
                updated_at=now,
                completed_at=None,
                error=None,
                next_action="RETRY_PHASE",
            )
            updated = replace(
                stored,
                snapshot=snapshot,
                phase_execution="IDLE",
                phase_attempt_id=None,
                lease_owner=None,
                lease_expires_at=None,
                stop_requested=False,
                cancel_requested=False,
            )
        else:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.INVALID_REQUEST,
                "unsupported acceptance mutation",
                run_id=request.run_id,
            )
        self.store.save_run(updated, connection=db)
        return _mutation_result(request, updated.snapshot, now), previous

    def _inspect_existing_mutation(
        self,
        request: AcceptanceMutationRequest,
        fingerprint: str,
        row: tuple[str, str, str | None, str | None, str, str | None],
    ) -> tuple[AcceptanceMutationResult | None, AcceptanceOwnerError | None]:
        stored_fingerprint, state, raw_result, raw_error, _, raw_completed = row
        if stored_fingerprint != fingerprint:
            return None, AcceptanceOwnerError(
                AcceptanceErrorCode.IDEMPOTENCY_CONFLICT,
                "request_id was already used for a different acceptance mutation",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        if state == "IN_PROGRESS":
            return None, AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "prior acceptance mutation outcome is uncertain",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        if state == "FAILED":
            try:
                code = AcceptanceErrorCode(str(raw_error))
            except ValueError:
                code = AcceptanceErrorCode.RECOVERY_REQUIRED
            return None, AcceptanceOwnerError(
                code,
                "replayed acceptance mutation failure",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        if state != "SUCCEEDED" or raw_result is None or raw_completed is None:
            return None, AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance mutation result is incomplete",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        try:
            payload = json.loads(raw_result)
            result = AcceptanceMutationResult(
                request_id=str(payload["request_id"]),
                operator=str(payload["operator"]),
                mutation=AcceptanceMutation(str(payload["mutation"])),
                acceptance_run_id=str(payload["acceptance_run_id"]),
                status=AcceptanceStatus(str(payload["status"])),
                phase=AcceptancePhase(str(payload["phase"])),
                completed_at=_parse_time(raw_completed),
                replayed=True,
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None, AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance mutation result is invalid",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        if result.request_id != request.request_id or result.mutation is not request.mutation:
            return None, AcceptanceOwnerError(
                AcceptanceErrorCode.RECOVERY_REQUIRED,
                "persisted acceptance mutation result identity is invalid",
                request_id=request.request_id,
                run_id=request.run_id,
            )
        return result, None

    def _audit_context(
        self,
        request: AcceptanceMutationRequest,
        result: AcceptanceMutationResult | None,
        error: AcceptanceOwnerError | None,
        db: sqlite3.Connection,
    ) -> tuple[str, AcceptanceStatus | None, AcceptancePhase]:
        if result is not None:
            return result.acceptance_run_id, result.status, result.phase
        candidate_run_id = error.run_id if error is not None else None
        candidate_run_id = candidate_run_id or request.run_id
        if candidate_run_id is not None:
            stored = self.store.load_run(candidate_run_id, connection=db)
            if stored is not None:
                return candidate_run_id, stored.snapshot.status, stored.snapshot.phase
        active = self.store.active_run(connection=db)
        if active is not None:
            return (
                active.snapshot.acceptance_run_id,
                active.snapshot.status,
                active.snapshot.phase,
            )
        return "unassigned", None, AcceptancePhase.ENVIRONMENT

    def _terminalize_requested_stop(
        self,
        stored: StoredRun,
        now: datetime,
        db: sqlite3.Connection,
    ) -> StoredRun:
        if stored.phase_execution != "IDLE" or not (
            stored.stop_requested or stored.cancel_requested
        ):
            return stored
        snapshot = replace(
            stored.snapshot,
            status=AcceptanceStatus.CANCELLED,
            updated_at=now,
            completed_at=now,
            error=None,
            next_action=None,
        )
        updated = replace(stored, snapshot=snapshot)
        self.store.save_run(updated, connection=db)
        return updated

    def _complete_phase_slice(
        self,
        attempt_id: str,
        outcome: PhaseOutcome,
    ) -> AcceptanceRunSnapshot:
        now = self._now()
        with self.store.transaction() as db:
            stored = self.store.active_run(connection=db)
            if stored is None:
                latest = self.store.latest_run()
                if latest is None:
                    raise AcceptanceOwnerError(
                        AcceptanceErrorCode.RUN_NOT_FOUND,
                        "acceptance run disappeared during phase completion",
                    )
                return self._with_wall_clock(latest.snapshot)
            if (
                stored.phase_execution != "IN_PROGRESS"
                or stored.phase_attempt_id != attempt_id
                or stored.lease_owner != self.instance_id
            ):
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.RECOVERY_REQUIRED,
                    "acceptance phase lease changed before completion",
                    run_id=stored.snapshot.acceptance_run_id,
                )
            if outcome.active_seconds > _MAX_ACTIVE_INCREMENT:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    "phase active time increment exceeds owner bound",
                    run_id=stored.snapshot.acceptance_run_id,
                )
            if (
                outcome.checkpoint is not None
                and outcome.checkpoint.phase is not stored.snapshot.phase
            ):
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    "phase checkpoint does not match current phase",
                    run_id=stored.snapshot.acceptance_run_id,
                )
            if outcome.status is PhaseOutcomeStatus.RUNNING and outcome.checkpoint is None:
                raise AcceptanceOwnerError(
                    AcceptanceErrorCode.INVALID_REQUEST,
                    "RUNNING phase outcome requires a durable checkpoint",
                    run_id=stored.snapshot.acceptance_run_id,
                )

            snapshot = stored.snapshot
            resources = _merge_resources(snapshot.resources, outcome.resources)
            evidence = _merge_evidence(snapshot.evidence, outcome.evidence)
            warnings = _merge_warnings(snapshot.warnings, outcome.warnings)
            active_time = snapshot.active_test_time + float(outcome.active_seconds)
            base_snapshot = replace(
                snapshot,
                updated_at=now,
                active_test_time=active_time,
                checkpoint=outcome.checkpoint or snapshot.checkpoint,
                resources=resources,
                warnings=warnings,
                evidence=evidence,
            )
            if outcome.status is PhaseOutcomeStatus.SUCCEEDED:
                phases = snapshot.spec.phases
                index = phases.index(snapshot.phase)
                if index == len(phases) - 1:
                    next_snapshot = replace(
                        base_snapshot,
                        status=AcceptanceStatus.SUCCEEDED,
                        completed_at=now,
                        error=None,
                        next_action=None,
                    )
                else:
                    next_snapshot = replace(
                        base_snapshot,
                        status=AcceptanceStatus.RUNNING,
                        phase=phases[index + 1],
                        error=None,
                        next_action="EXECUTE_PHASE",
                    )
            elif outcome.status is PhaseOutcomeStatus.RUNNING:
                next_snapshot = replace(
                    base_snapshot,
                    status=AcceptanceStatus.RUNNING,
                    error=None,
                    next_action=outcome.next_action or "CONTINUE_PHASE",
                )
            elif outcome.status is PhaseOutcomeStatus.BLOCKED:
                next_snapshot = replace(
                    base_snapshot,
                    status=AcceptanceStatus.BLOCKED,
                    completed_at=now,
                    error=outcome.error_code or AcceptanceErrorCode.PHASE_BLOCKED.value,
                    next_action=outcome.next_action or "OWNER_REVIEW",
                )
            elif outcome.status is PhaseOutcomeStatus.CANCELLED:
                next_snapshot = replace(
                    base_snapshot,
                    status=AcceptanceStatus.CANCELLED,
                    completed_at=now,
                    error=None,
                    next_action=None,
                )
            else:
                next_snapshot = replace(
                    base_snapshot,
                    status=AcceptanceStatus.FAILED,
                    completed_at=now,
                    error=outcome.error_code or AcceptanceErrorCode.PHASE_FAILURE.value,
                    next_action=outcome.next_action or "RETRY_PHASE",
                )

            if (stored.cancel_requested or stored.stop_requested) and next_snapshot.status not in {
                AcceptanceStatus.FAILED,
                AcceptanceStatus.BLOCKED,
            }:
                next_snapshot = replace(
                    next_snapshot,
                    status=AcceptanceStatus.CANCELLED,
                    completed_at=now,
                    error=None,
                    next_action=None,
                )
            updated = replace(
                stored,
                snapshot=next_snapshot,
                phase_execution="IDLE",
                phase_attempt_id=None,
                lease_owner=None,
                lease_expires_at=None,
            )
            self.store.save_run(updated, connection=db)
            return self._with_wall_clock(next_snapshot)

    def _with_wall_clock(self, snapshot: AcceptanceRunSnapshot) -> AcceptanceRunSnapshot:
        end = snapshot.completed_at or self._now()
        elapsed = max(0.0, (end - snapshot.created_at).total_seconds())
        return replace(snapshot, wall_clock_elapsed=elapsed)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AcceptanceOwnerError(
                AcceptanceErrorCode.OWNER_FAILURE,
                "acceptance owner clock is invalid",
            )
        return value.astimezone(UTC)


def _fingerprint(request: AcceptanceMutationRequest) -> str:
    payload: dict[str, object] = {
        "mutation": request.mutation.value,
        "operator": request.operator.subject,
        "run_id": request.run_id,
    }
    if request.spec is not None:
        payload["spec"] = {
            "full_rc_sha": request.spec.full_rc_sha,
            "dataset_id": request.spec.dataset_id,
            "dataset_digest": request.spec.dataset_digest,
            "strategy_id": request.spec.strategy_id,
            "strategy_digest": request.spec.strategy_digest,
            "config_digest": request.spec.config_digest,
            "enable_soak_24h": request.spec.enable_soak_24h,
            "enable_soak_72h": request.spec.enable_soak_72h,
            "mode": request.spec.mode,
        }
    material = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _run_id(request_id: str, fingerprint: str) -> str:
    digest = hashlib.sha256(f"{request_id}:{fingerprint}".encode("utf-8")).hexdigest()[:24]
    return f"acceptance-{digest}"


def _attempt_id(run_id: str, phase: AcceptancePhase, attempt: int) -> str:
    digest = hashlib.sha256(f"{run_id}:{phase.value}:{attempt}".encode("utf-8")).hexdigest()[:24]
    return f"attempt-{digest}"


def _phase_context(stored: StoredRun, *, recovering: bool) -> PhaseExecutionContext:
    snapshot = stored.snapshot
    if stored.phase_attempt_id is None:
        raise AcceptanceOwnerError(
            AcceptanceErrorCode.RECOVERY_REQUIRED,
            "phase attempt identity is unavailable",
            run_id=snapshot.acceptance_run_id,
        )
    checkpoint = (
        snapshot.checkpoint
        if snapshot.checkpoint is not None and snapshot.checkpoint.phase is snapshot.phase
        else None
    )
    return PhaseExecutionContext(
        acceptance_run_id=snapshot.acceptance_run_id,
        phase=snapshot.phase,
        attempt_id=stored.phase_attempt_id,
        recovering=recovering,
        full_rc_sha=snapshot.spec.full_rc_sha,
        dataset_id=snapshot.spec.dataset_id,
        dataset_digest=snapshot.spec.dataset_digest,
        strategy_id=snapshot.spec.strategy_id,
        strategy_digest=snapshot.spec.strategy_digest,
        config_digest=snapshot.spec.config_digest,
        active_test_time=snapshot.active_test_time,
        checkpoint_token=checkpoint.token if checkpoint is not None else None,
        checkpoint_digest=checkpoint.digest if checkpoint is not None else None,
    )


def _mutation_result(
    request: AcceptanceMutationRequest,
    snapshot: AcceptanceRunSnapshot,
    now: datetime,
) -> AcceptanceMutationResult:
    return AcceptanceMutationResult(
        request_id=request.request_id,
        operator=request.operator.subject,
        mutation=request.mutation,
        acceptance_run_id=snapshot.acceptance_run_id,
        status=snapshot.status,
        phase=snapshot.phase,
        completed_at=now,
        replayed=False,
    )


def _mutation_result_mapping(result: AcceptanceMutationResult) -> dict[str, object]:
    return {
        "request_id": result.request_id,
        "operator": result.operator,
        "mutation": result.mutation.value,
        "acceptance_run_id": result.acceptance_run_id,
        "status": result.status.value,
        "phase": result.phase.value,
    }


def _merge_resources(
    existing: tuple[ResourceSample, ...], incoming: tuple[ResourceSample, ...]
) -> tuple[ResourceSample, ...]:
    return (*existing, *incoming)[-_MAX_RESOURCES:]


def _merge_warnings(existing: tuple[str, ...], incoming: tuple[str, ...]) -> tuple[str, ...]:
    return (*existing, *incoming)[-_MAX_WARNINGS:]


def _merge_evidence(
    existing: tuple[EvidenceReference, ...], incoming: tuple[EvidenceReference, ...]
) -> tuple[EvidenceReference, ...]:
    merged: list[EvidenceReference] = list(existing)
    identities = {(item.kind, item.digest, item.reference) for item in merged}
    for item in incoming:
        identity = (item.kind, item.digest, item.reference)
        if identity not in identities:
            merged.append(item)
            identities.add(identity)
    return tuple(merged[-_MAX_EVIDENCE:])


def _parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp is naive")
    return parsed.astimezone(UTC)


def _safe_identifier(value: str) -> bool:
    return (
        1 <= len(value) <= 128
        and value[0].isalnum()
        and all(character.isalnum() or character in "._:@-" for character in value)
    )
