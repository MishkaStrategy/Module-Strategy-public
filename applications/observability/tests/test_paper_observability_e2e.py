from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from modular_risk_supervisor import (
    AuthoritativeRiskEvidence,
    RiskSupervisor,
    RiskSupervisorPolicy,
)
from modular_trading_runner import RuntimeState, TradingRunner, TradingRunnerConfig
from modular_trading_runner.enums import HealthStatus
from modular_trading_runner.supervision import RuntimeHealthRegistry, SupervisionPolicy

from modular_observability import (
    AlertEngine,
    AlertSeverity,
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    ObservabilityService,
    RiskSupervisorSource,
    TradingRunnerSource,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)
KEY = b"observability-paper-e2e-key-32-bytes!"


class MemoryStore:
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


class Journal:
    def append(self, **kwargs: object) -> None:
        del kwargs


class Planner:
    def validate_and_plan(
        self,
        modules: Sequence[Any],
        *,
        base_timeframe: str,
    ) -> object:
        assert base_timeframe == "5m"
        return SimpleNamespace(modules=tuple(modules))


class SuccessfulBoundary:
    def recover(self) -> bool:
        return True

    def reconcile(self) -> bool:
        return True


class MarketDataBoundary:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class PlanBoundary:
    def accept(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        return object()


class CheckpointBoundary:
    def capture(self) -> None:
        return None


def _risk_evidence(
    snapshot_id: str,
    observed_at: datetime,
    *,
    reconciliation_loss_count: int,
) -> AuthoritativeRiskEvidence:
    return AuthoritativeRiskEvidence(
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        source_state_ids={
            "portfolio": f"portfolio-{snapshot_id}",
            "positions": f"positions-{snapshot_id}",
            "paper": f"paper-{snapshot_id}",
            "market": f"market-{snapshot_id}",
            "runner": f"runner-{snapshot_id}",
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
        disk_free_bytes=10_000_000_000,
        transport_healthy=True,
        execution_latency_seconds=0.5,
    )


def _policy() -> ObservabilityPolicy:
    return ObservabilityPolicy(
        freshness_seconds={
            Component.RISK_SUPERVISOR: 30.0,
            Component.TRADING_RUNNER: 30.0,
        },
        required_components=frozenset({Component.RISK_SUPERVISOR, Component.TRADING_RUNNER}),
        critical_components=frozenset({Component.RISK_SUPERVISOR, Component.TRADING_RUNNER}),
        alert_cooldown_seconds=30.0,
        alert_state_capacity=2,
    )


@pytest.mark.asyncio
async def test_real_paper_owners_project_halt_without_mutating_runner_authority() -> None:
    runner_store = MemoryStore()
    runner_clock = Clock(NOW)
    runner_health = RuntimeHealthRegistry(
        policy=SupervisionPolicy(heartbeat_timeout_seconds=30.0),
        clock=runner_clock,
    )
    runner_health.register("market_data", critical=True)
    runner_health.heartbeat("market_data", status=HealthStatus.HEALTHY, timestamp=NOW)
    runner = TradingRunner(
        config=TradingRunnerConfig.from_mapping(
            {
                "schema_version": "1",
                "runtime_id": "runner-observability-e2e",
                "portfolio_id": "portfolio-main",
                "strategy_id": "strategy-a",
                "execution_mode": "paper",
                "base_timeframe": "5m",
                "market": "usdt_perpetual",
                "instruments": ("BTCUSDT",),
                "market_data_sources": ("paper-source",),
                "modules": (),
            }
        ),
        modules=(),
        planner=Planner(),
        clock=runner_clock,
        journal=Journal(),
        state_store=runner_store,
        recovery=SuccessfulBoundary(),
        reconciliation=SuccessfulBoundary(),
        health=runner_health,
        market_data=MarketDataBoundary(),
        plan_execution=PlanBoundary(),
        checkpoint=CheckpointBoundary(),
    )
    await runner.start()
    assert runner.state is RuntimeState.RUNNING
    assert not runner.kill_switch.active

    risk_store = MemoryStore()
    risk = RiskSupervisor(
        runtime_id="risk-observability-e2e",
        policy=RiskSupervisorPolicy(),
        repository=risk_store,
        operator_hmac_key=KEY,
    )
    risk.evaluate(
        _risk_evidence("snapshot-1", NOW, reconciliation_loss_count=0),
        now=NOW,
    )
    service = ObservabilityService(
        policy=_policy(),
        sources=(
            RiskSupervisorSource(owner=risk),
            TradingRunnerSource(runner=runner, health_query=runner_health),
        ),
    )

    healthy = service.snapshot(now=NOW)
    assert healthy.overall_health is ComponentHealth.HEALTHY

    fault_time = NOW + timedelta(seconds=1)
    risk.evaluate(
        _risk_evidence("snapshot-2", fault_time, reconciliation_loss_count=1),
        now=fault_time,
    )
    runner_saves_before = runner_store.save_calls
    risk_saves_before = risk_store.save_calls

    halted = service.snapshot(now=fault_time)
    alerts = AlertEngine(policy=_policy()).evaluate(halted)

    risk_component = next(
        item for item in halted.components if item.component is Component.RISK_SUPERVISOR
    )
    runner_component = next(
        item for item in halted.components if item.component is Component.TRADING_RUNNER
    )
    assert halted.overall_health is ComponentHealth.BLOCKED
    assert risk_component.health is ComponentHealth.BLOCKED
    assert "risk_supervisor.reconciliation_loss" in risk_component.reason_codes
    assert runner_component.health is ComponentHealth.HEALTHY
    assert len(alerts) == 1
    assert alerts[0].component is Component.RISK_SUPERVISOR
    assert alerts[0].severity is AlertSeverity.CRITICAL
    assert alerts[0].next_action == "run_reconciliation_recovery"

    assert runner.state is RuntimeState.RUNNING
    assert not runner.kill_switch.active
    assert runner_store.save_calls == runner_saves_before
    assert risk_store.save_calls == risk_saves_before
