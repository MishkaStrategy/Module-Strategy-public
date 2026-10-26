from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_observability import (
    AlertEngine,
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    SourceObservation,
    SourceRead,
    build_overview,
    opaque_state_ref,
)

START = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _policy() -> ObservabilityPolicy:
    return ObservabilityPolicy.paper_default(freshness_seconds=30.0)


def _run_sequence() -> tuple[tuple[str, ...], tuple[str, ...], int]:
    policy = _policy()
    engine = AlertEngine(policy=policy)
    overview_hashes: list[str] = []
    event_ids: list[str] = []
    maximum_active = 0

    for index in range(256):
        moment = START + timedelta(seconds=index)
        reads: dict[Component, SourceRead] = {}
        for component in Component:
            health = ComponentHealth.HEALTHY
            reasons: tuple[str, ...] = ()
            progress_at = moment
            if component is Component.MARKET_DATA and 50 <= index % 128 < 60:
                health = ComponentHealth.DEGRADED
                reasons = ("market_data.transport_unhealthy",)
            if component is Component.RISK_SUPERVISOR and 90 <= index % 128 < 100:
                health = ComponentHealth.BLOCKED
                reasons = ("risk_supervisor.resource_pressure",)
            if component is Component.BACKTEST_RESEARCH and 110 <= index % 128 < 115:
                progress_at = moment - timedelta(seconds=301)
            if component is Component.PAPER_EXECUTION and 120 <= index % 128 < 125:
                reads[component] = SourceRead(
                    component=component,
                    observation=None,
                    unavailable_reason="owner_unavailable",
                )
                continue
            observation = SourceObservation(
                component=component,
                health=health,
                observed_at=moment,
                progress_at=progress_at,
                state_ref=opaque_state_ref(
                    {
                        "component": component.value,
                        "health": health.value,
                        "reasons": list(reasons),
                        "epoch": index // 5,
                    }
                ),
                reason_codes=reasons,
                correlation_id=f"{component.value}-soak",
            )
            reads[component] = SourceRead(component=component, observation=observation)

        overview = build_overview(reads, now=moment, policy=policy)
        overview_hashes.append(overview.overview_hash)
        event_ids.extend(event.event_id for event in engine.evaluate(overview))
        maximum_active = max(maximum_active, engine.active_count)
        assert engine.active_count <= len(Component)

        if index and index % 64 == 0:
            engine = AlertEngine(policy=policy)

    return tuple(overview_hashes), tuple(event_ids), maximum_active


def test_deterministic_fault_soak_is_bounded_and_replay_stable() -> None:
    first = _run_sequence()
    second = _run_sequence()

    assert first == second
    assert len(first[0]) == 256
    assert first[2] <= len(Component)
    assert first[1]
