from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from modular_risk_supervisor import (
    AuthoritativeRiskEvidence,
    RiskSupervisor,
    RiskSupervisorPolicy,
    SafetyState,
)

START = datetime(2026, 8, 28, tzinfo=UTC)
OPERATOR_KEY = b"risk-supervisor-deterministic-soak-key"
SNAPSHOT_COUNT = 128


class Store:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


def evidence(index: int, observed_at: datetime) -> AuthoritativeRiskEvidence:
    identity = f"soak-{index:04d}"
    return AuthoritativeRiskEvidence(
        snapshot_id=identity,
        observed_at=observed_at,
        source_state_ids={
            "portfolio": f"portfolio-{identity}",
            "positions": f"positions-{identity}",
            "paper": f"paper-{identity}",
            "market": f"market-{identity}",
            "runner": f"runner-{identity}",
        },
        session_start_equity=Decimal("1000"),
        current_equity=Decimal("995"),
        peak_equity=Decimal("1000"),
        aggregate_exposure_notional=Decimal("1000"),
        market_data_observed_at=observed_at,
        consumer_progress_at=observed_at,
        reconciliation_blocked_scopes=0,
        reconciliation_loss_count=0,
        execution_attempts=100,
        execution_rejections=1,
        cancel_attempts=100,
        cancel_failures=0,
        fill_samples=100,
        mean_abs_slippage_bps=Decimal("3"),
        fill_quality_fraction=Decimal("0.99"),
        checkpoint_integrity_ok=True,
        disk_free_bytes=10_000_000_000,
        transport_healthy=True,
        execution_latency_seconds=0.25,
    )


def build(store: Store) -> RiskSupervisor:
    return RiskSupervisor(
        runtime_id="paper-runtime-soak",
        policy=RiskSupervisorPolicy(),
        repository=store,
        operator_hmac_key=OPERATOR_KEY,
    )


def test_bounded_deterministic_restart_soak_preserves_decision_identity_and_replay() -> None:
    store = Store()
    owner = build(store)
    final_hash = ""

    for index in range(SNAPSHOT_COUNT):
        observed_at = START + timedelta(seconds=index)
        snapshot = evidence(index, observed_at)
        decision = owner.evaluate(snapshot, now=observed_at)

        assert decision.state is SafetyState.HEALTHY
        assert decision.allow_risk_increase
        assert decision.sequence == index + 1
        assert decision.snapshot_hash == snapshot.content_hash
        assert owner.breach_counts == {}
        final_hash = decision.decision_hash

        if index % 16 == 0:
            audit_size = len(owner.audit_log)
            replay = owner.evaluate(snapshot, now=observed_at + timedelta(seconds=1))
            assert replay == decision
            assert replay.decision_hash == decision.decision_hash
            assert len(owner.audit_log) == audit_size

        if index % 16 == 15:
            owner = build(store)
            restored = owner.decision(now=observed_at)
            assert restored == decision
            assert restored.decision_hash == decision.decision_hash
            assert owner.breach_counts == {}

    restored = build(store)
    final = restored.decision(now=START + timedelta(seconds=SNAPSHOT_COUNT - 1))

    assert final.state is SafetyState.HEALTHY
    assert final.allow_risk_increase
    assert final.sequence == SNAPSHOT_COUNT
    assert final.decision_hash == final_hash
    assert len(restored.audit_log) == SNAPSHOT_COUNT
    assert restored.breach_counts == {}
