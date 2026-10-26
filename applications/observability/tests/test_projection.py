from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_observability import (
    CallableObservationSource,
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    ObservabilityService,
    SourceObservation,
    SourceRead,
    build_overview,
    opaque_state_ref,
    overview_mapping,
    project_component,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _policy() -> ObservabilityPolicy:
    return ObservabilityPolicy(
        freshness_seconds={
            Component.MARKET_DATA: 30.0,
            Component.BACKTEST_RESEARCH: 30.0,
        },
        stuck_seconds={Component.BACKTEST_RESEARCH: 60.0},
        required_components=frozenset({Component.MARKET_DATA, Component.BACKTEST_RESEARCH}),
        critical_components=frozenset({Component.MARKET_DATA}),
        alert_cooldown_seconds=30.0,
        alert_state_capacity=2,
    )


def _observation(
    component: Component,
    *,
    observed_at: datetime = NOW,
    progress_at: datetime | None = None,
    health: ComponentHealth = ComponentHealth.HEALTHY,
    reasons: tuple[str, ...] = (),
    public_details: dict[str, object] | None = None,
) -> SourceObservation:
    return SourceObservation(
        component=component,
        health=health,
        observed_at=observed_at,
        progress_at=progress_at,
        state_ref=opaque_state_ref(
            {
                "component": component.value,
                "observed_at": observed_at.isoformat(),
                "health": health.value,
            }
        ),
        reason_codes=reasons,
        correlation_id="paper-runtime-1",
        public_details=public_details or {},  # type: ignore[arg-type]
    )


def test_policy_rejects_missing_freshness_and_undersized_alert_state() -> None:
    with pytest.raises(ValueError, match="freshness policy missing"):
        ObservabilityPolicy(
            freshness_seconds={Component.MARKET_DATA: 30.0},
            required_components=frozenset({Component.MARKET_DATA, Component.BACKTEST_RESEARCH}),
            critical_components=frozenset({Component.MARKET_DATA}),
        )

    with pytest.raises(ValueError, match="alert_state_capacity"):
        ObservabilityPolicy(
            freshness_seconds={
                Component.MARKET_DATA: 30.0,
                Component.BACKTEST_RESEARCH: 30.0,
            },
            required_components=frozenset({Component.MARKET_DATA, Component.BACKTEST_RESEARCH}),
            critical_components=frozenset({Component.MARKET_DATA}),
            alert_state_capacity=1,
        )


def test_stale_required_owner_is_unavailable_and_never_green() -> None:
    read = SourceRead(
        component=Component.MARKET_DATA,
        observation=_observation(
            Component.MARKET_DATA,
            observed_at=NOW - timedelta(seconds=31),
        ),
    )

    projected = project_component(read, now=NOW, policy=_policy())

    assert projected.health is ComponentHealth.UNAVAILABLE
    assert projected.stale
    assert projected.reason_codes == ("source_stale",)


def test_future_owner_timestamp_fails_closed() -> None:
    read = SourceRead(
        component=Component.MARKET_DATA,
        observation=_observation(
            Component.MARKET_DATA,
            observed_at=NOW + timedelta(seconds=3),
        ),
    )

    projected = project_component(read, now=NOW, policy=_policy())

    assert projected.health is ComponentHealth.UNAVAILABLE
    assert projected.reason_codes == ("source_clock_anomaly",)


def test_server_owned_stuck_threshold_degrades_without_mutating_owner_truth() -> None:
    source = _observation(
        Component.BACKTEST_RESEARCH,
        progress_at=NOW - timedelta(seconds=61),
    )
    read = SourceRead(component=Component.BACKTEST_RESEARCH, observation=source)

    projected = project_component(read, now=NOW, policy=_policy())

    assert source.health is ComponentHealth.HEALTHY
    assert source.reason_codes == ()
    assert projected.health is ComponentHealth.DEGRADED
    assert projected.stuck
    assert projected.reason_codes == ("source_progress_stalled",)


def test_missing_required_owner_makes_overall_unavailable() -> None:
    market = SourceRead(
        component=Component.MARKET_DATA,
        observation=_observation(Component.MARKET_DATA),
    )

    overview = build_overview(
        {Component.MARKET_DATA: market},
        now=NOW,
        policy=_policy(),
    )

    by_component = {item.component: item for item in overview.components}
    assert overview.overall_health is ComponentHealth.UNAVAILABLE
    assert by_component[Component.MARKET_DATA].health is ComponentHealth.HEALTHY
    assert by_component[Component.BACKTEST_RESEARCH].health is ComponentHealth.UNAVAILABLE
    assert by_component[Component.BACKTEST_RESEARCH].reason_codes == ("owner_unavailable",)
    assert by_component[Component.BACKTEST_RESEARCH].public_details == {}


def test_same_owner_truth_and_same_explicit_time_is_deterministic() -> None:
    reads = {
        Component.MARKET_DATA: SourceRead(
            component=Component.MARKET_DATA,
            observation=_observation(Component.MARKET_DATA),
        ),
        Component.BACKTEST_RESEARCH: SourceRead(
            component=Component.BACKTEST_RESEARCH,
            observation=_observation(Component.BACKTEST_RESEARCH, progress_at=NOW),
        ),
    }

    first = build_overview(reads, now=NOW, policy=_policy())
    second = build_overview(reads, now=NOW, policy=_policy())

    assert first == second
    assert first.overview_hash == second.overview_hash
    assert overview_mapping(first) == overview_mapping(second)


def test_public_details_are_immutable_projected_serialized_and_hash_bound() -> None:
    source = _observation(
        Component.MARKET_DATA,
        public_details={
            "gap_count": 2,
            "ingestion_ready": True,
            "queue_fill_fraction": 0.25,
            "source_mode": "paper",
        },
    )
    first = project_component(
        SourceRead(component=Component.MARKET_DATA, observation=source),
        now=NOW,
        policy=_policy(),
    )
    second_source = _observation(
        Component.MARKET_DATA,
        public_details={
            "gap_count": 3,
            "ingestion_ready": True,
            "queue_fill_fraction": 0.25,
            "source_mode": "paper",
        },
    )
    second = project_component(
        SourceRead(component=Component.MARKET_DATA, observation=second_source),
        now=NOW,
        policy=_policy(),
    )

    assert dict(first.public_details) == {
        "gap_count": 2,
        "ingestion_ready": True,
        "queue_fill_fraction": 0.25,
        "source_mode": "paper",
    }
    assert (
        overview_mapping(
            build_overview(
                {
                    Component.MARKET_DATA: SourceRead(
                        component=Component.MARKET_DATA, observation=source
                    )
                },
                now=NOW,
                policy=_policy(),
            )
        )["components"][1]["public_details"]
        == first.public_details
    )
    assert first.observation_hash != second.observation_hash
    with pytest.raises(TypeError):
        first.public_details["gap_count"] = 9  # type: ignore[index]


@pytest.mark.parametrize(
    ("details", "error"),
    [
        ({f"metric_{index}": index for index in range(33)}, ValueError),
        ({"nested": {"secret": "value"}}, TypeError),
        ({"not_finite": float("inf")}, ValueError),
        ({"too_large": 2**63}, ValueError),
        ({"bad_key!": 1}, ValueError),
        ({"text": "line\nbreak"}, ValueError),
    ],
)
def test_public_details_reject_unbounded_or_structured_payloads(
    details: dict[str, object],
    error: type[Exception],
) -> None:
    with pytest.raises(error):
        _observation(Component.MARKET_DATA, public_details=details)


def test_service_converts_throwing_owner_to_unavailable_without_persistence_side_effect() -> None:
    class ThrowingQuery:
        def __init__(self) -> None:
            self.save_calls = 0

        def __call__(self, *, now: datetime) -> SourceObservation:
            del now
            raise RuntimeError("owner storage is corrupt")

    query = ThrowingQuery()
    market_source = CallableObservationSource(
        component=Component.MARKET_DATA,
        query=lambda *, now: _observation(Component.MARKET_DATA, observed_at=now),
    )
    broken = CallableObservationSource(
        component=Component.BACKTEST_RESEARCH,
        query=query,
    )
    service = ObservabilityService(policy=_policy(), sources=(market_source, broken))

    first = service.snapshot(now=NOW)
    second = service.snapshot(now=NOW)

    item = next(
        component
        for component in first.components
        if component.component is Component.BACKTEST_RESEARCH
    )
    assert item.health is ComponentHealth.UNAVAILABLE
    assert item.reason_codes == ("owner_read_failed",)
    assert first.overview_hash == second.overview_hash
    assert query.save_calls == 0


def test_service_rejects_duplicate_component_sources() -> None:
    source = CallableObservationSource(
        component=Component.MARKET_DATA,
        query=lambda *, now: _observation(Component.MARKET_DATA, observed_at=now),
    )

    with pytest.raises(ValueError, match="duplicate observation source"):
        ObservabilityService(policy=_policy(), sources=(source, source))
