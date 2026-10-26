from __future__ import annotations

import json
from pathlib import Path
from typing import cast

import pytest

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptancePhase,
    AcceptanceStatus,
    PhaseExecutionContext,
    PhaseOutcome,
    PhaseOutcomeStatus,
)
from modular_system_acceptance.bootstrap import (
    ProductionSystemAcceptanceProcess,
    SystemAcceptanceBootstrapConfig,
)
from modular_system_acceptance.executors import CheckpointedSoakExecutor
from modular_system_acceptance.production_composition import TrustedProductionPhaseAdapters
from modular_system_acceptance.production_identity import TrustedAcceptanceIdentity

FULL_RC = "a" * 40
DATASET_DIGEST = "b" * 64
STRATEGY_DIGEST = "c" * 64
CONFIG_DIGEST = "d" * 64
READER = "reader-token-0123456789"
OPERATOR = "operator-token-0123456789"


class SuccessfulExecutor:
    def execute(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)

    def recover(self, context: PhaseExecutionContext) -> PhaseOutcome:
        return PhaseOutcome(status=PhaseOutcomeStatus.SUCCEEDED)


def identity() -> TrustedAcceptanceIdentity:
    return TrustedAcceptanceIdentity(
        full_rc_sha=FULL_RC,
        dataset_id="dataset-v1",
        dataset_digest=DATASET_DIGEST,
        strategy_id="strategy-v1",
        strategy_digest=STRATEGY_DIGEST,
        config_digest=CONFIG_DIGEST,
    )


def config(path: Path) -> SystemAcceptanceBootstrapConfig:
    return SystemAcceptanceBootstrapConfig(
        identity=identity(),
        state_path=path,
        instance_id="bootstrap-recovery-owner",
        reader_token=READER,
        operator_token=OPERATOR,
        port=0,
        poll_seconds=0.05,
        lease_seconds=5,
    )


def start_body(request_id: str) -> bytes:
    return json.dumps(
        {
            "request_id": request_id,
            "spec": {
                "dataset_id": "dataset-v1",
                "dataset_digest": DATASET_DIGEST,
                "strategy_id": "strategy-v1",
                "strategy_digest": STRATEGY_DIGEST,
                "config_digest": CONFIG_DIGEST,
                "enable_soak_24h": False,
                "enable_soak_72h": False,
            },
        }
    ).encode()


def stub_phases(monkeypatch: pytest.MonkeyPatch) -> TrustedProductionPhaseAdapters:
    executor = SuccessfulExecutor()
    registry = AcceptanceExecutorRegistry({phase: executor for phase in AcceptancePhase})
    monkeypatch.setattr(
        TrustedProductionPhaseAdapters,
        "build_registry",
        lambda self, *, soak: registry,
    )
    return cast(
        TrustedProductionPhaseAdapters,
        object.__new__(TrustedProductionPhaseAdapters),
    )


def build_process(
    path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> ProductionSystemAcceptanceProcess:
    return ProductionSystemAcceptanceProcess(
        config=config(path),
        phases=stub_phases(monkeypatch),
        soak=CheckpointedSoakExecutor(lambda _: True),
    )


def test_bootstrap_reopens_same_durable_run_after_process_restart(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "acceptance.db"
    first = build_process(path, monkeypatch)
    try:
        code, _, _ = first._http.response(  # noqa: SLF001 - verifies composed transport
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body("restart-start"),
        )
        assert code == 200
        before = first.owner.snapshot()
        assert before is not None
        run_id = before.acceptance_run_id
        assert before.restart_count == 0
    finally:
        first.close()

    second = build_process(path, monkeypatch)
    try:
        recovered = second.owner.recover_after_restart()
        assert recovered is not None
        assert recovered.acceptance_run_id == run_id
        assert recovered.restart_count == 1
        assert recovered.status is AcceptanceStatus.RECOVERING
        assert second.owner.snapshot(run_id) is not None
    finally:
        second.close()


def test_bootstrap_rejects_second_active_start_with_distinct_request_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    process = build_process(tmp_path / "acceptance.db", monkeypatch)
    try:
        first_code, _, _ = process._http.response(  # noqa: SLF001
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body("first-start"),
        )
        second_code, _, second_body = process._http.response(  # noqa: SLF001
            method="POST",
            path="/acceptance/v1/start",
            authorization=f"Bearer {OPERATOR}",
            body=start_body("second-start"),
        )
        assert first_code == 200
        assert second_code == 409
        payload = json.loads(second_body)
        assert payload["error"] == "RUN_ALREADY_ACTIVE"
    finally:
        process.close()
