from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

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

KEY = b"risk-supervisor-test-key-32-bytes!!"
NOW = datetime(2026, 8, 28, tzinfo=UTC)


class Store:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


def policy(**overrides: object) -> RiskSupervisorPolicy:
    values: dict[str, object] = {
        "maximum_daily_loss_fraction": Decimal("0.03"),
        "maximum_drawdown_fraction": Decimal("0.06"),
        "maximum_exposure_notional": Decimal("10000"),
        "maximum_gross_leverage": Decimal("3"),
        "maximum_rejection_rate_fraction": Decimal("0.20"),
        "maximum_cancel_failure_rate_fraction": Decimal("0.10"),
        "minimum_execution_samples": 10,
        "maximum_mean_abs_slippage_bps": Decimal("25"),
        "minimum_fill_quality_fraction": Decimal("0.80"),
        "maximum_evidence_age_seconds": 15,
        "maximum_market_data_age_seconds": 15,
        "maximum_consumer_stall_seconds": 30,
        "maximum_execution_latency_seconds": 5,
        "minimum_disk_free_bytes": 1000,
        "maximum_future_skew_seconds": 2,
    }
    values.update(overrides)
    return RiskSupervisorPolicy(**values)  # type: ignore[arg-type]


def healthy_evidence(
    *,
    snapshot_id: str = "snapshot-1",
    observed_at: datetime = NOW,
    **overrides: object,
) -> AuthoritativeRiskEvidence:
    values: dict[str, object] = {
        "snapshot_id": snapshot_id,
        "observed_at": observed_at,
        "source_state_ids": {
            "portfolio": "portfolio-v10",
            "positions": "positions-v20",
            "paper": "paper-v30",
            "market": "market-v40",
            "runner": "runner-v50",
        },
        "session_start_equity": Decimal("1000"),
        "current_equity": Decimal("995"),
        "peak_equity": Decimal("1000"),
        "aggregate_exposure_notional": Decimal("1000"),
        "market_data_observed_at": observed_at,
        "consumer_progress_at": observed_at,
        "reconciliation_blocked_scopes": 0,
        "reconciliation_loss_count": 0,
        "execution_attempts": 20,
        "execution_rejections": 1,
        "cancel_attempts": 20,
        "cancel_failures": 1,
        "fill_samples": 20,
        "mean_abs_slippage_bps": Decimal("5"),
        "fill_quality_fraction": Decimal("0.95"),
        "checkpoint_integrity_ok": True,
        "disk_free_bytes": 10_000,
        "transport_healthy": True,
        "execution_latency_seconds": 0.5,
    }
    values.update(overrides)
    return AuthoritativeRiskEvidence(**values)  # type: ignore[arg-type]


def supervisor(store: Store | None = None, **policy_overrides: object) -> RiskSupervisor:
    return RiskSupervisor(
        runtime_id="paper-runtime-1",
        policy=policy(**policy_overrides),
        repository=store or Store(),
        operator_hmac_key=KEY,
    )


def command(
    command_id: str,
    action: OperatorAction,
    *,
    issued_at: datetime = NOW,
    key: bytes = KEY,
) -> OperatorCommand:
    signature = sign_operator_command(
        key,
        command_id=command_id,
        runtime_id="paper-runtime-1",
        action=action,
        issued_at=issued_at,
    )
    return OperatorCommand(
        command_id=command_id,
        runtime_id="paper-runtime-1",
        action=action,
        issued_at=issued_at,
        signature=signature,
    )


def test_uninitialized_supervisor_is_fail_closed() -> None:
    decision = supervisor().decision(now=NOW)

    assert decision.state is SafetyState.HALT
    assert decision.allow_risk_increase is False
    assert decision.guard_codes == (RiskGuardCode.SUPERVISOR_UNINITIALIZED,)


def test_healthy_factual_snapshot_yields_hashed_healthy_decision() -> None:
    evidence = healthy_evidence()
    owner = supervisor()

    decision = owner.evaluate(evidence, now=NOW)

    assert decision.state is SafetyState.HEALTHY
    assert decision.allow_risk_increase is True
    assert decision.snapshot_hash == evidence.content_hash
    assert decision.source_state_ids == evidence.source_state_ids
    assert len(decision.decision_hash) == 64
    assert owner.audit_log[-1]["event_type"] == "evidence_evaluated"


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        (
            {"current_equity": Decimal("960"), "peak_equity": Decimal("1000")},
            RiskGuardCode.DAILY_LOSS_LIMIT,
        ),
        (
            {
                "session_start_equity": Decimal("1100"),
                "current_equity": Decimal("930"),
                "peak_equity": Decimal("1000"),
            },
            RiskGuardCode.DRAWDOWN_LIMIT,
        ),
        (
            {"aggregate_exposure_notional": Decimal("10000")},
            RiskGuardCode.EXPOSURE_NOTIONAL_LIMIT,
        ),
        (
            {"aggregate_exposure_notional": Decimal("4000")},
            RiskGuardCode.LEVERAGE_LIMIT,
        ),
        (
            {"market_data_observed_at": NOW - timedelta(seconds=16)},
            RiskGuardCode.MARKET_DATA_STALE,
        ),
        (
            {"consumer_progress_at": NOW - timedelta(seconds=31)},
            RiskGuardCode.CONSUMER_STALLED,
        ),
        ({"reconciliation_blocked_scopes": 1}, RiskGuardCode.RECONCILIATION_BLOCKED),
        ({"reconciliation_loss_count": 1}, RiskGuardCode.RECONCILIATION_LOSS),
        ({"execution_rejections": 4}, RiskGuardCode.REJECTION_RATE_LIMIT),
        ({"cancel_failures": 2}, RiskGuardCode.CANCEL_FAILURE_RATE_LIMIT),
        ({"mean_abs_slippage_bps": Decimal("25")}, RiskGuardCode.SLIPPAGE_LIMIT),
        ({"fill_quality_fraction": Decimal("0.80")}, RiskGuardCode.FILL_QUALITY_LIMIT),
        ({"checkpoint_integrity_ok": False}, RiskGuardCode.CHECKPOINT_INTEGRITY_FAILURE),
        ({"disk_free_bytes": 1000}, RiskGuardCode.RESOURCE_PRESSURE),
        ({"transport_healthy": False}, RiskGuardCode.TRANSPORT_UNHEALTHY),
        ({"execution_latency_seconds": 5}, RiskGuardCode.EXECUTION_LATENCY_LIMIT),
    ],
)
def test_each_mandatory_guard_halts_new_risk(
    overrides: dict[str, object], expected: RiskGuardCode
) -> None:
    owner = supervisor()

    decision = owner.evaluate(healthy_evidence(**overrides), now=NOW)

    assert decision.state is SafetyState.HALT
    assert decision.allow_risk_increase is False
    assert expected in decision.guard_codes
    assert RiskGuardCode.RECOVERY_REQUIRED in decision.guard_codes
    assert owner.breach_counts[expected] == 1


def test_stale_whole_snapshot_halts_without_dependency_callback() -> None:
    owner = supervisor()
    evidence = healthy_evidence(observed_at=NOW - timedelta(seconds=16))

    decision = owner.evaluate(evidence, now=NOW)

    assert RiskGuardCode.EVIDENCE_STALE in decision.guard_codes
    assert decision.state is SafetyState.HALT


def test_compound_failures_are_reported_together_deterministically() -> None:
    owner = supervisor()
    evidence = healthy_evidence(
        current_equity=Decimal("900"),
        peak_equity=Decimal("1100"),
        aggregate_exposure_notional=Decimal("12000"),
        reconciliation_blocked_scopes=2,
        execution_rejections=8,
        checkpoint_integrity_ok=False,
        transport_healthy=False,
    )

    decision = owner.evaluate(evidence, now=NOW)

    assert decision.state is SafetyState.HALT
    assert tuple(sorted(decision.guard_codes, key=str)) == decision.guard_codes
    assert {
        RiskGuardCode.DAILY_LOSS_LIMIT,
        RiskGuardCode.DRAWDOWN_LIMIT,
        RiskGuardCode.EXPOSURE_NOTIONAL_LIMIT,
        RiskGuardCode.LEVERAGE_LIMIT,
        RiskGuardCode.RECONCILIATION_BLOCKED,
        RiskGuardCode.REJECTION_RATE_LIMIT,
        RiskGuardCode.CHECKPOINT_INTEGRITY_FAILURE,
        RiskGuardCode.TRANSPORT_UNHEALTHY,
        RiskGuardCode.RECOVERY_REQUIRED,
    }.issubset(decision.guard_codes)


def test_duplicate_snapshot_replay_is_exactly_idempotent() -> None:
    owner = supervisor()
    evidence = healthy_evidence()
    first = owner.evaluate(evidence, now=NOW)
    audit_size = len(owner.audit_log)

    replay = owner.evaluate(evidence, now=NOW + timedelta(seconds=1))

    assert replay == first
    assert replay.decision_hash == first.decision_hash
    assert len(owner.audit_log) == audit_size


def test_same_snapshot_id_different_content_fails_closed_without_mutating_state() -> None:
    owner = supervisor()
    first = owner.evaluate(healthy_evidence(), now=NOW)
    audit_size = len(owner.audit_log)

    mismatch = owner.evaluate(
        healthy_evidence(current_equity=Decimal("994")),
        now=NOW,
    )

    assert mismatch.state is SafetyState.MANUAL_REVIEW
    assert mismatch.guard_codes == (RiskGuardCode.SNAPSHOT_ID_CONTENT_MISMATCH,)
    assert len(owner.audit_log) == audit_size
    assert owner.decision(now=NOW).sequence == first.sequence


def test_out_of_order_snapshot_fails_closed_without_mutation() -> None:
    owner = supervisor()
    first = owner.evaluate(healthy_evidence(observed_at=NOW), now=NOW)

    out_of_order = owner.evaluate(
        healthy_evidence(
            snapshot_id="snapshot-old",
            observed_at=NOW - timedelta(seconds=1),
            market_data_observed_at=NOW - timedelta(seconds=1),
            consumer_progress_at=NOW - timedelta(seconds=1),
        ),
        now=NOW,
    )

    assert out_of_order.state is SafetyState.MANUAL_REVIEW
    assert out_of_order.guard_codes == (RiskGuardCode.SNAPSHOT_OUT_OF_ORDER,)
    assert owner.decision(now=NOW).sequence == first.sequence


def test_future_clock_anomaly_fails_closed_without_mutation() -> None:
    owner = supervisor()
    future = NOW + timedelta(seconds=3)

    decision = owner.evaluate(
        healthy_evidence(
            observed_at=future,
            market_data_observed_at=future,
            consumer_progress_at=future,
        ),
        now=NOW,
    )

    assert decision.state is SafetyState.MANUAL_REVIEW
    assert decision.guard_codes == (RiskGuardCode.CLOCK_ANOMALY,)
    assert owner.audit_log == ()


def test_mandatory_halt_is_latched_until_authenticated_recovery() -> None:
    owner = supervisor()
    halted = owner.evaluate(
        healthy_evidence(snapshot_id="bad", transport_healthy=False),
        now=NOW,
    )
    healthy_after = owner.evaluate(
        healthy_evidence(snapshot_id="good", observed_at=NOW + timedelta(seconds=1)),
        now=NOW + timedelta(seconds=1),
    )

    assert halted.state is SafetyState.HALT
    assert healthy_after.state is SafetyState.HALT
    assert RiskGuardCode.TRANSPORT_UNHEALTHY in healthy_after.guard_codes
    assert RiskGuardCode.RECOVERY_REQUIRED in healthy_after.guard_codes

    recovered = owner.apply_operator_command(
        command("recover-1", OperatorAction.RECOVER, issued_at=NOW + timedelta(seconds=1)),
        now=NOW + timedelta(seconds=1),
    )

    assert recovered.state is SafetyState.HEALTHY
    assert recovered.allow_risk_increase is True
    assert owner.audit_log[-1]["event_type"] == "operator_recovery"


def test_recovery_is_rejected_while_current_evidence_is_unhealthy() -> None:
    owner = supervisor()
    owner.evaluate(healthy_evidence(transport_healthy=False), now=NOW)

    decision = owner.apply_operator_command(
        command("recover-1", OperatorAction.RECOVER),
        now=NOW,
    )

    assert decision.state is SafetyState.MANUAL_REVIEW
    assert RiskGuardCode.TRANSPORT_UNHEALTHY in decision.guard_codes
    assert RiskGuardCode.RECOVERY_REQUIRED in decision.guard_codes


def test_authenticated_emergency_halt_is_durable_audited_and_idempotent() -> None:
    store = Store()
    owner = supervisor(store)
    owner.evaluate(healthy_evidence(), now=NOW)
    halt = command("halt-1", OperatorAction.HALT)

    first = owner.apply_operator_command(halt, now=NOW)
    replay = owner.apply_operator_command(halt, now=NOW + timedelta(seconds=1))
    restored = supervisor(store)

    assert first.state is SafetyState.HALT
    assert RiskGuardCode.OPERATOR_EMERGENCY_HALT in first.guard_codes
    assert replay == first
    assert restored.decision(now=NOW + timedelta(seconds=1)).state is SafetyState.HALT
    assert (
        RiskGuardCode.OPERATOR_EMERGENCY_HALT
        in restored.decision(now=NOW + timedelta(seconds=1)).guard_codes
    )
    assert restored.audit_log[-1]["event_type"] == "operator_halt"


def test_invalid_operator_signature_cannot_change_owner_state() -> None:
    owner = supervisor()
    healthy = owner.evaluate(healthy_evidence(), now=NOW)
    bad = command("halt-bad", OperatorAction.HALT, key=b"different-operator-key-32-bytes!!!")

    decision = owner.apply_operator_command(bad, now=NOW)

    assert decision.state is SafetyState.MANUAL_REVIEW
    assert decision.guard_codes == (RiskGuardCode.OPERATOR_AUTH_FAILED,)
    assert owner.decision(now=NOW).sequence == healthy.sequence
    assert owner.decision(now=NOW).state is SafetyState.HEALTHY


def test_operator_command_same_id_different_content_fails_closed() -> None:
    owner = supervisor()
    owner.evaluate(healthy_evidence(), now=NOW)
    owner.apply_operator_command(command("operator-1", OperatorAction.HALT), now=NOW)

    collision = owner.apply_operator_command(
        command("operator-1", OperatorAction.RECOVER),
        now=NOW,
    )

    assert collision.state is SafetyState.MANUAL_REVIEW
    assert collision.guard_codes == (RiskGuardCode.OPERATOR_COMMAND_REPLAY_MISMATCH,)


def test_restart_restores_latched_halt_counters_and_audit_exactly() -> None:
    store = Store()
    owner = supervisor(store)
    owner.evaluate(
        healthy_evidence(transport_healthy=False, reconciliation_blocked_scopes=1),
        now=NOW,
    )
    counters = dict(owner.breach_counts)
    audit = tuple(dict(row) for row in owner.audit_log)

    restored = supervisor(store)

    assert restored.decision(now=NOW).state is SafetyState.HALT
    assert dict(restored.breach_counts) == counters
    assert tuple(dict(row) for row in restored.audit_log) == audit


def test_corrupt_persisted_state_recovers_fail_closed() -> None:
    store = Store()
    owner = supervisor(store)
    owner.evaluate(healthy_evidence(), now=NOW)
    store.values[owner.state_key]["sequence"] = 999

    restored = supervisor(store)

    assert restored.decision(now=NOW).state is SafetyState.MANUAL_REVIEW
    assert restored.decision(now=NOW).guard_codes == (RiskGuardCode.STATE_INVALID,)


def test_staleness_after_restart_cannot_reuse_old_allow_decision() -> None:
    store = Store()
    owner = supervisor(store)
    owner.evaluate(healthy_evidence(), now=NOW)
    restored = supervisor(store)

    decision = restored.decision(now=NOW + timedelta(seconds=16))

    assert decision.state is SafetyState.HALT
    assert RiskGuardCode.EVIDENCE_STALE in decision.guard_codes
    assert RiskGuardCode.MARKET_DATA_STALE in decision.guard_codes


def test_nonmandatory_guard_degrades_without_allowing_it_to_become_authority() -> None:
    mandatory = frozenset(
        code for code in policy().mandatory_guards if code is not RiskGuardCode.TRANSPORT_UNHEALTHY
    )
    owner = supervisor(mandatory_guards=mandatory)

    decision = owner.evaluate(healthy_evidence(transport_healthy=False), now=NOW)

    assert decision.state is SafetyState.DEGRADED
    assert decision.allow_risk_increase is False
    assert decision.guard_codes == (RiskGuardCode.TRANSPORT_UNHEALTHY,)
