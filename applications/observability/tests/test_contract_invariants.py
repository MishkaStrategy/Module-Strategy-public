from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_observability import (
    AlertEngine,
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    OperationalOverview,
    SourceObservation,
    SourceRead,
    build_overview,
    opaque_state_ref,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _policy(*components: Component) -> ObservabilityPolicy:
    required = frozenset(components)
    return ObservabilityPolicy(
        freshness_seconds={component: 30.0 for component in required},
        required_components=required,
        critical_components=required,
        alert_state_capacity=len(required),
    )


def _read(component: Component, health: ComponentHealth) -> SourceRead:
    reasons = () if health is ComponentHealth.HEALTHY else ("owner_fault",)
    observation = SourceObservation(
        component=component,
        health=health,
        observed_at=NOW,
        state_ref=opaque_state_ref({"component": component.value, "health": health.value}),
        reason_codes=reasons,
    )
    return SourceRead(component=component, observation=observation)


def test_operational_overview_rejects_green_summary_over_blocked_component() -> None:
    policy = _policy(Component.RISK_SUPERVISOR)
    valid = build_overview(
        {Component.RISK_SUPERVISOR: _read(Component.RISK_SUPERVISOR, ComponentHealth.BLOCKED)},
        now=NOW,
        policy=policy,
    )

    with pytest.raises(ValueError, match="overall_health"):
        OperationalOverview(
            evaluated_at=NOW,
            overall_health=ComponentHealth.HEALTHY,
            components=valid.components,
        )


def test_operational_overview_rejects_mixed_evaluation_times() -> None:
    policy = _policy(Component.MARKET_DATA)
    valid = build_overview(
        {Component.MARKET_DATA: _read(Component.MARKET_DATA, ComponentHealth.HEALTHY)},
        now=NOW,
        policy=policy,
    )
    component = valid.components[0]

    with pytest.raises(ValueError, match="evaluated_at"):
        OperationalOverview(
            evaluated_at=NOW + timedelta(seconds=1),
            overall_health=ComponentHealth.HEALTHY,
            components=(component,),
        )


def test_alert_engine_rejects_overview_missing_required_component() -> None:
    policy = _policy(Component.MARKET_DATA, Component.RISK_SUPERVISOR)
    partial_policy = _policy(Component.MARKET_DATA)
    partial = build_overview(
        {Component.MARKET_DATA: _read(Component.MARKET_DATA, ComponentHealth.HEALTHY)},
        now=NOW,
        policy=partial_policy,
    )
    engine = AlertEngine(policy=policy)

    with pytest.raises(ValueError, match="required component set"):
        engine.evaluate(partial)

    assert engine.active_count == 0
