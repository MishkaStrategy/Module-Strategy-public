from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from modular_risk_supervisor import (
    AuthoritativeRiskEvidence,
    OperatorAction,
    OperatorCommand,
    RiskGuardCode,
    RiskSupervisor,
    RiskSupervisorPolicy,
    SafetyState,
    sign_operator_command,
)

NOW = datetime(2026, 8, 28, tzinfo=UTC)
KEY = b"pre-evidence-operator-halt-test-key!!"


class Store:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


def owner(store: Store) -> RiskSupervisor:
    return RiskSupervisor(
        runtime_id="paper-pre-evidence",
        policy=RiskSupervisorPolicy(),
        repository=store,
        operator_hmac_key=KEY,
    )


def command(command_id: str, action: OperatorAction, issued_at: datetime) -> OperatorCommand:
    return OperatorCommand(
        command_id=command_id,
        runtime_id="paper-pre-evidence",
        action=action,
        issued_at=issued_at,
        signature=sign_operator_command(
            KEY,
            command_id=command_id,
            runtime_id="paper-pre-evidence",
            action=action,
            issued_at=issued_at,
        ),
    )


def healthy_evidence(observed_at: datetime) -> AuthoritativeRiskEvidence:
    return AuthoritativeRiskEvidence(
        snapshot_id="first-authoritative-snapshot",
        observed_at=observed_at,
        source_state_ids={
            "portfolio": "portfolio-v1",
            "positions": "positions-v1",
            "paper": "paper-v1",
            "market": "market-v1",
            "runner": "runner-v1",
        },
        session_start_equity=Decimal("1000"),
        current_equity=Decimal("995"),
        peak_equity=Decimal("1000"),
        aggregate_exposure_notional=Decimal("1000"),
        market_data_observed_at=observed_at,
        consumer_progress_at=observed_at,
        reconciliation_blocked_scopes=0,
        reconciliation_loss_count=0,
        execution_attempts=20,
        execution_rejections=1,
        cancel_attempts=20,
        cancel_failures=0,
        fill_samples=20,
        mean_abs_slippage_bps=Decimal("5"),
        fill_quality_fraction=Decimal("0.95"),
        checkpoint_integrity_ok=True,
        disk_free_bytes=10_000_000_000,
        transport_healthy=True,
        execution_latency_seconds=0.5,
    )


def test_operator_halt_is_durable_before_first_snapshot_and_recovery_waits_for_evidence() -> None:
    store = Store()
    first = owner(store)
    halt = command("halt-before-evidence", OperatorAction.HALT, NOW)

    halted = first.apply_operator_command(halt, now=NOW)
    restored = owner(store)
    current = restored.decision(now=NOW + timedelta(seconds=1))

    assert halted.state is SafetyState.HALT
    assert halted.snapshot_id is None
    assert RiskGuardCode.OPERATOR_EMERGENCY_HALT in halted.guard_codes
    assert current.state is SafetyState.HALT
    assert RiskGuardCode.OPERATOR_EMERGENCY_HALT in current.guard_codes
    assert RiskGuardCode.SUPERVISOR_UNINITIALIZED in current.guard_codes
    assert restored.audit_log[-1]["event_type"] == "operator_halt"
    assert restored.apply_operator_command(halt, now=NOW + timedelta(seconds=1)) == halted

    blocked_recovery = restored.apply_operator_command(
        command("recover-too-early", OperatorAction.RECOVER, NOW + timedelta(seconds=1)),
        now=NOW + timedelta(seconds=1),
    )
    assert blocked_recovery.state is SafetyState.MANUAL_REVIEW
    assert RiskGuardCode.SUPERVISOR_UNINITIALIZED in blocked_recovery.guard_codes
    assert RiskGuardCode.RECOVERY_REQUIRED in blocked_recovery.guard_codes

    snapshot_time = NOW + timedelta(seconds=2)
    after_evidence = restored.evaluate(healthy_evidence(snapshot_time), now=snapshot_time)
    assert after_evidence.state is SafetyState.HALT
    assert RiskGuardCode.OPERATOR_EMERGENCY_HALT in after_evidence.guard_codes

    recovered = restored.apply_operator_command(
        command("recover-after-evidence", OperatorAction.RECOVER, snapshot_time),
        now=snapshot_time,
    )
    assert recovered.state is SafetyState.HEALTHY
    assert recovered.allow_risk_increase
