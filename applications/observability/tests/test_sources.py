from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from modular_risk_supervisor import (
    AuthoritativeRiskEvidence,
    RiskSupervisor,
    RiskSupervisorPolicy,
)
from modular_trading_runner import RuntimeState
from modular_trading_runner.enums import HealthStatus
from modular_trading_runner.supervision import RuntimeHealthRegistry, SupervisionPolicy

from modular_observability import (
    ComponentHealth,
    RiskSupervisorSource,
    TradingRunnerSource,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
KEY = b"observability-risk-supervisor-key-32-bytes"


class Store:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}
        self.save_calls = 0

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.save_calls += 1
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


class Clock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _evidence(
    *,
    snapshot_id: str = "snapshot-1",
    observed_at: datetime = NOW,
    reconciliation_loss_count: int = 0,
    disk_free_bytes: int = 10_000,
) -> AuthoritativeRiskEvidence:
    return AuthoritativeRiskEvidence(
        snapshot_id=snapshot_id,
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
        reconciliation_loss_count=reconciliation_loss_count,
        execution_attempts=20,
        execution_rejections=1,
        cancel_attempts=20,
        cancel_failures=1,
        fill_samples=20,
        mean_abs_slippage_bps=Decimal("5"),
        fill_quality_fraction=Decimal("0.95"),
        checkpoint_integrity_ok=True,
        disk_free_bytes=disk_free_bytes,
        transport_healthy=True,
        execution_latency_seconds=0.5,
    )


def _supervisor(store: Store) -> RiskSupervisor:
    return RiskSupervisor(
        runtime_id="paper-runtime-observability",
        policy=RiskSupervisorPolicy(minimum_disk_free_bytes=1000),
        repository=store,
        operator_hmac_key=KEY,
    )


def test_risk_supervisor_source_is_read_only_and_has_stable_owner_state_ref() -> None:
    store = Store()
    owner = _supervisor(store)
    owner.evaluate(_evidence(), now=NOW)
    before = store.save_calls
    source = RiskSupervisorSource(owner=owner)

    first = source.read(now=NOW)
    second = source.read(now=NOW + timedelta(seconds=1))

    assert first.observation is not None
    assert second.observation is not None
    assert first.observation.health is ComponentHealth.HEALTHY
    assert second.observation.health is ComponentHealth.HEALTHY
    assert first.observation.state_ref == second.observation.state_ref
    assert first.observation.correlation_id == "paper-runtime-observability"
    assert first.observation.public_details == {
        "guard_count": 0,
        "sequence": 1,
        "source_state_count": 5,
        "state": "healthy",
    }
    assert store.save_calls == before


def test_risk_supervisor_reconciliation_loss_is_projected_not_recomputed() -> None:
    store = Store()
    owner = _supervisor(store)
    owner.evaluate(_evidence(reconciliation_loss_count=1), now=NOW)

    read = RiskSupervisorSource(owner=owner).read(now=NOW)

    assert read.observation is not None
    assert read.observation.health is ComponentHealth.BLOCKED
    assert "risk_supervisor.reconciliation_loss" in read.observation.reason_codes
    assert "risk_supervisor.recovery_required" in read.observation.reason_codes
    assert read.observation.public_details["state"] == "halt"
    assert read.observation.public_details["guard_count"] >= 2


def test_risk_supervisor_resource_pressure_is_projected_as_blocked() -> None:
    store = Store()
    owner = _supervisor(store)
    owner.evaluate(_evidence(disk_free_bytes=999), now=NOW)

    read = RiskSupervisorSource(owner=owner).read(now=NOW)

    assert read.observation is not None
    assert read.observation.health is ComponentHealth.BLOCKED
    assert "risk_supervisor.resource_pressure" in read.observation.reason_codes


def test_uninitialized_risk_supervisor_is_not_green() -> None:
    owner = _supervisor(Store())

    read = RiskSupervisorSource(owner=owner).read(now=NOW)

    assert read.observation is not None
    assert read.observation.health is ComponentHealth.BLOCKED
    assert read.observation.reason_codes == ("risk_supervisor.supervisor_uninitialized",)
    assert read.observation.public_details["sequence"] == 0


def _runner_source(*, heartbeat_timeout_seconds: float = 5.0) -> tuple[TradingRunnerSource, Clock]:
    clock = Clock(NOW)
    health = RuntimeHealthRegistry(
        policy=SupervisionPolicy(heartbeat_timeout_seconds=heartbeat_timeout_seconds),
        clock=clock,
    )
    health.register("market_data", critical=True)
    health.heartbeat("market_data", status=HealthStatus.HEALTHY, timestamp=NOW)
    runner = SimpleNamespace(
        state=RuntimeState.RUNNING,
        kill_switch=SimpleNamespace(active=False),
        config=SimpleNamespace(runtime_id="runner-observability"),
    )
    return TradingRunnerSource(runner=runner, health_query=health), clock


def test_runner_native_health_snapshot_projects_healthy_running_state() -> None:
    source, _ = _runner_source()

    read = source.read(now=NOW)

    assert read.observation is not None
    assert read.observation.health is ComponentHealth.HEALTHY
    assert read.observation.reason_codes == ()
    assert read.observation.correlation_id == "runner-observability"
    assert read.observation.public_details == {
        "critical_component_count": 1,
        "health_component_count": 1,
        "health_live": True,
        "health_ready": True,
        "kill_switch_active": False,
        "state": "running",
        "total_consecutive_failures": 0,
    }


def test_dead_runner_heartbeat_projects_blocked_without_changing_owner_registry() -> None:
    source, clock = _runner_source()
    clock.value = NOW + timedelta(seconds=6)

    read = source.read(now=clock.value)

    assert read.observation is not None
    assert read.observation.health is ComponentHealth.BLOCKED
    assert "runner.critical_health_failure" in read.observation.reason_codes
    assert "runner.health_not_live" in read.observation.reason_codes
    assert read.observation.public_details["health_live"] is False


def test_runner_kill_switch_is_projected_as_blocked() -> None:
    source, _ = _runner_source()
    source.runner.kill_switch.active = True  # type: ignore[attr-defined]

    read = source.read(now=NOW)

    assert read.observation is not None
    assert read.observation.health is ComponentHealth.BLOCKED
    assert "runner.kill_switch_active" in read.observation.reason_codes
    assert read.observation.public_details["kill_switch_active"] is True
