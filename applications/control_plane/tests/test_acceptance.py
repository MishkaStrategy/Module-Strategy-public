from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from modular_control_plane.acceptance import (
    AcceptanceCandidate,
    AcceptanceControlService,
    AcceptanceCurrent,
    AcceptanceEvidenceReference,
    AcceptanceMutation,
    AcceptanceMutationResult,
    AcceptancePhase,
    AcceptanceResourceSample,
    AcceptanceRunMutationRequest,
    AcceptanceRunSnapshot,
    AcceptanceStartRequest,
    AcceptanceStatus,
)
from modular_control_plane.contracts import HealthStatus

NOW = datetime(2026, 8, 9, 6, 30, tzinfo=UTC)
FULL_RC = "a" * 40
DATASET = "b" * 64
STRATEGY = "c" * 64
CONFIG = "d" * 64
EVIDENCE = "e" * 64


def candidate() -> AcceptanceCandidate:
    return AcceptanceCandidate(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY,
        config_digest=CONFIG,
        mode="PAPER",
        enable_soak_24h=True,
        enable_soak_72h=False,
    )


def snapshot(
    *,
    status: AcceptanceStatus = AcceptanceStatus.RUNNING,
    phase: AcceptancePhase = AcceptancePhase.SOAK_2H,
) -> AcceptanceRunSnapshot:
    terminal = status.terminal
    return AcceptanceRunSnapshot(
        acceptance_run_id="acceptance-v1",
        status=status,
        phase=phase,
        created_at=NOW,
        started_at=NOW + timedelta(seconds=1),
        updated_at=NOW + timedelta(seconds=20),
        completed_at=NOW + timedelta(seconds=20) if terminal else None,
        active_test_time=12.5,
        wall_clock_elapsed=20.0,
        restart_count=1,
        checkpoint=None,
        resources=(
            AcceptanceResourceSample(
                timestamp=NOW + timedelta(seconds=10),
                cpu_percent=12.0,
                ram_bytes=1024,
                disk_free_bytes=4096,
            ),
        ),
        warnings=("PROBE_DEGRADED",),
        error="PHASE_BLOCKED" if status is AcceptanceStatus.BLOCKED else None,
        next_action="OWNER_REVIEW" if status is AcceptanceStatus.BLOCKED else "CONTINUE",
        evidence=(
            AcceptanceEvidenceReference(
                kind="acceptance.checkpoint",
                digest=EVIDENCE,
                reference="checkpoint:abc123",
            ),
        ),
        identity=candidate(),
        phases=(
            AcceptancePhase.ENVIRONMENT,
            AcceptancePhase.DATASET,
            AcceptancePhase.BACKTEST_A,
            AcceptancePhase.BACKTEST_B,
            AcceptancePhase.DETERMINISM,
            AcceptancePhase.RESEARCH,
            AcceptancePhase.RUNTIME_SMOKE,
            AcceptancePhase.RECOVERY,
            AcceptancePhase.SOAK_2H,
            AcceptancePhase.SOAK_24H,
            AcceptancePhase.FINAL_VERIFICATION,
        ),
    )


class FakeClient:
    def __init__(self, current: AcceptanceCurrent, *, operator_available: bool = True) -> None:
        self._current = current
        self._operator_available = operator_available
        self.calls: list[tuple[str, str]] = []

    @property
    def operator_available(self) -> bool:
        return self._operator_available

    def current(self) -> AcceptanceCurrent:
        self.calls.append(("current", ""))
        return self._current

    def start(self, request: AcceptanceStartRequest) -> AcceptanceMutationResult:
        self.calls.append(("start", request.request_id))
        return receipt(AcceptanceMutation.START, request.request_id)

    def stop(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        self.calls.append(("stop", request.request_id))
        return receipt(AcceptanceMutation.STOP, request.request_id)

    def cancel(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        self.calls.append(("cancel", request.request_id))
        return receipt(AcceptanceMutation.CANCEL, request.request_id)

    def retry(self, request: AcceptanceRunMutationRequest) -> AcceptanceMutationResult:
        self.calls.append(("retry", request.request_id))
        return receipt(AcceptanceMutation.RETRY, request.request_id)


def receipt(mutation: AcceptanceMutation, request_id: str) -> AcceptanceMutationResult:
    return AcceptanceMutationResult(
        request_id=request_id,
        operator="control-plane:local-operator",
        mutation=mutation,
        acceptance_run_id="acceptance-v1",
        status=AcceptanceStatus.RUNNING,
        phase=AcceptancePhase.SOAK_2H,
        completed_at=NOW,
        replayed=False,
    )


def start_request() -> AcceptanceStartRequest:
    return AcceptanceStartRequest(
        request_id="acceptance-start-1",
        dataset_id="dataset-v1",
        dataset_digest=DATASET,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY,
        config_digest=CONFIG,
        enable_soak_24h=True,
        enable_soak_72h=False,
    )


def test_health_preserves_active_and_wall_clock_semantics() -> None:
    client = FakeClient(AcceptanceCurrent(run=snapshot()))
    control = AcceptanceControlService(client, clock=lambda: NOW)

    result = control.health()

    assert result.status is HealthStatus.HEALTHY
    assert result.details["current_run"] is True
    assert result.details["mutations_available"] is True
    assert result.details["active_test_time"] == 12.5
    assert result.details["wall_clock_elapsed"] == 20.0
    assert result.details["restart_count"] == 1


def test_failed_or_blocked_run_degrades_component_without_losing_owner_truth() -> None:
    current = snapshot(status=AcceptanceStatus.BLOCKED)
    control = AcceptanceControlService(
        FakeClient(AcceptanceCurrent(run=current)), clock=lambda: NOW
    )

    result = control.health()

    assert result.status is HealthStatus.DEGRADED
    assert result.details["run_status"] == "BLOCKED"
    assert result.details["phase"] == "SOAK_2H"


def test_no_current_run_is_a_healthy_attached_owner() -> None:
    control = AcceptanceControlService(
        FakeClient(AcceptanceCurrent(run=None), operator_available=False),
        clock=lambda: NOW,
    )

    result = control.health()

    assert result.status is HealthStatus.HEALTHY
    assert result.details == {
        "current_run": False,
        "mutations_available": False,
        "owner_attached": True,
    }


def test_service_forwards_same_request_identity_without_local_mutation_state() -> None:
    client = FakeClient(AcceptanceCurrent(run=None))
    control = AcceptanceControlService(client)
    request = start_request()

    first = control.start(request)
    second = control.start(request)

    assert first.request_id == second.request_id == "acceptance-start-1"
    assert client.calls == [
        ("start", "acceptance-start-1"),
        ("start", "acceptance-start-1"),
    ]


def test_candidate_rejects_live_mode_and_invalid_digests() -> None:
    data = candidate().model_dump()
    data["mode"] = "LIVE"
    with pytest.raises(ValidationError):
        AcceptanceCandidate.model_validate(data)

    data = candidate().model_dump()
    data["config_digest"] = "/Users/private/config.yaml"
    with pytest.raises(ValidationError):
        AcceptanceCandidate.model_validate(data)


def test_start_request_forbids_browser_full_rc_mode_and_unknown_fields() -> None:
    data = start_request().model_dump()
    data["full_rc_sha"] = FULL_RC
    with pytest.raises(ValidationError):
        AcceptanceStartRequest.model_validate(data)

    data = start_request().model_dump()
    data["mode"] = "PAPER"
    with pytest.raises(ValidationError):
        AcceptanceStartRequest.model_validate(data)


def test_start_request_enforces_soak_dependency_and_request_id_bounds() -> None:
    data = start_request().model_dump()
    data["enable_soak_24h"] = False
    data["enable_soak_72h"] = True
    with pytest.raises(ValidationError):
        AcceptanceStartRequest.model_validate(data)

    data = start_request().model_dump()
    data["request_id"] = "bad/request"
    with pytest.raises(ValidationError):
        AcceptanceStartRequest.model_validate(data)


def test_snapshot_rejects_terminal_time_and_phase_inconsistency() -> None:
    data = snapshot().model_dump()
    data["status"] = "SUCCEEDED"
    data["completed_at"] = None
    with pytest.raises(ValidationError):
        AcceptanceRunSnapshot.model_validate(data)

    data = snapshot().model_dump()
    data["phases"] = ["ENVIRONMENT", "DATASET"]
    with pytest.raises(ValidationError):
        AcceptanceRunSnapshot.model_validate(data)


def test_snapshot_bounds_resource_warning_and_evidence_collections() -> None:
    data = snapshot().model_dump()
    data["warnings"] = ["W"] * 33
    with pytest.raises(ValidationError):
        AcceptanceRunSnapshot.model_validate(data)

    data = snapshot().model_dump()
    data["evidence"] = [data["evidence"][0]] * 65
    with pytest.raises(ValidationError):
        AcceptanceRunSnapshot.model_validate(data)
