from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_observability import (
    AlertEngine,
    AlertSeverity,
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    SourceObservation,
    SourceRead,
    build_overview,
    opaque_state_ref,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _policy() -> ObservabilityPolicy:
    return ObservabilityPolicy.paper_default(freshness_seconds=30.0)


def _observation(
    component: Component,
    *,
    observed_at: datetime = NOW,
    progress_at: datetime | None = NOW,
    health: ComponentHealth = ComponentHealth.HEALTHY,
    reasons: tuple[str, ...] = (),
    version: str = "v1",
) -> SourceObservation:
    return SourceObservation(
        component=component,
        health=health,
        observed_at=observed_at,
        progress_at=progress_at,
        state_ref=opaque_state_ref(
            {
                "component": component.value,
                "version": version,
                "health": health.value,
                "reasons": list(reasons),
            }
        ),
        reason_codes=reasons,
        correlation_id=f"{component.value}-public-1",
    )


def _healthy_reads() -> dict[Component, SourceRead]:
    return {
        component: SourceRead(component=component, observation=_observation(component))
        for component in Component
    }


def test_full_component_matrix_can_be_truthfully_healthy() -> None:
    overview = build_overview(_healthy_reads(), now=NOW, policy=_policy())

    assert len(overview.components) == len(Component)
    assert overview.overall_health is ComponentHealth.HEALTHY
    assert all(item.health is ComponentHealth.HEALTHY for item in overview.components)


@pytest.mark.parametrize(
    ("component", "read", "expected_health", "reason"),
    [
        (
            Component.MARKET_DATA,
            SourceRead(
                component=Component.MARKET_DATA,
                observation=_observation(
                    Component.MARKET_DATA,
                    observed_at=NOW - timedelta(seconds=31),
                ),
            ),
            ComponentHealth.UNAVAILABLE,
            "source_stale",
        ),
        (
            Component.BACKTEST_RESEARCH,
            SourceRead(
                component=Component.BACKTEST_RESEARCH,
                observation=_observation(
                    Component.BACKTEST_RESEARCH,
                    progress_at=NOW - timedelta(seconds=301),
                ),
            ),
            ComponentHealth.DEGRADED,
            "source_progress_stalled",
        ),
        (
            Component.PAPER_EXECUTION,
            SourceRead(
                component=Component.PAPER_EXECUTION,
                observation=_observation(
                    Component.PAPER_EXECUTION,
                    health=ComponentHealth.BLOCKED,
                    reasons=("paper.reconciliation_loss",),
                ),
            ),
            ComponentHealth.BLOCKED,
            "paper.reconciliation_loss",
        ),
        (
            Component.SYSTEM_RESOURCES,
            SourceRead(
                component=Component.SYSTEM_RESOURCES,
                observation=_observation(
                    Component.SYSTEM_RESOURCES,
                    health=ComponentHealth.DEGRADED,
                    reasons=("system_resources.resource_pressure",),
                ),
            ),
            ComponentHealth.DEGRADED,
            "system_resources.resource_pressure",
        ),
        (
            Component.STRATEGY_AUDIT_FACTORY,
            SourceRead(
                component=Component.STRATEGY_AUDIT_FACTORY,
                observation=_observation(
                    Component.STRATEGY_AUDIT_FACTORY,
                    health=ComponentHealth.DEGRADED,
                    reasons=("strategy_audit.failed",),
                ),
            ),
            ComponentHealth.DEGRADED,
            "strategy_audit.failed",
        ),
        (
            Component.RISK_SUPERVISOR,
            SourceRead(
                component=Component.RISK_SUPERVISOR,
                observation=_observation(
                    Component.RISK_SUPERVISOR,
                    health=ComponentHealth.BLOCKED,
                    reasons=("risk_supervisor.operator_emergency_halt",),
                ),
            ),
            ComponentHealth.BLOCKED,
            "risk_supervisor.operator_emergency_halt",
        ),
        (
            Component.DATASET_REGISTRY,
            SourceRead(
                component=Component.DATASET_REGISTRY,
                observation=None,
                unavailable_reason="owner_unavailable",
            ),
            ComponentHealth.UNAVAILABLE,
            "owner_unavailable",
        ),
    ],
)
def test_fault_matrix_never_masks_owner_faults(
    component: Component,
    read: SourceRead,
    expected_health: ComponentHealth,
    reason: str,
) -> None:
    reads = _healthy_reads()
    reads[component] = read

    overview = build_overview(reads, now=NOW, policy=_policy())
    item = next(value for value in overview.components if value.component is component)
    alerts = AlertEngine(policy=_policy()).evaluate(overview)

    assert item.health is expected_health
    assert reason in item.reason_codes
    assert overview.overall_health is not ComponentHealth.HEALTHY
    alert = next(value for value in alerts if value.component is component)
    if component in _policy().critical_components or expected_health in {
        ComponentHealth.BLOCKED,
        ComponentHealth.UNAVAILABLE,
    }:
        assert alert.severity is AlertSeverity.CRITICAL


def test_signal_bot_delivery_failure_never_changes_execution_truth() -> None:
    reads = _healthy_reads()
    reads[Component.SIGNAL_BOT] = SourceRead(
        component=Component.SIGNAL_BOT,
        observation=_observation(
            Component.SIGNAL_BOT,
            health=ComponentHealth.DEGRADED,
            reasons=("signal_bot.delivery_failed",),
        ),
    )

    overview = build_overview(reads, now=NOW, policy=_policy())
    runner = next(
        item for item in overview.components if item.component is Component.TRADING_RUNNER
    )
    signal_bot = next(
        item for item in overview.components if item.component is Component.SIGNAL_BOT
    )

    assert signal_bot.health is ComponentHealth.DEGRADED
    assert runner.health is ComponentHealth.HEALTHY
    assert overview.overall_health is ComponentHealth.DEGRADED
