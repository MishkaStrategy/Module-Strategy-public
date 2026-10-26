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
    active_alert_mapping,
    build_overview,
    opaque_state_ref,
)

NOW = datetime(2026, 8, 28, 4, 0, tzinfo=UTC)


def _policy() -> ObservabilityPolicy:
    return ObservabilityPolicy(
        freshness_seconds={Component.RISK_SUPERVISOR: 300.0},
        required_components=frozenset({Component.RISK_SUPERVISOR}),
        critical_components=frozenset({Component.RISK_SUPERVISOR}),
        alert_cooldown_seconds=30.0,
        alert_state_capacity=1,
    )


def _overview(*, now: datetime, state_version: str, healthy: bool = False):
    observation = SourceObservation(
        component=Component.RISK_SUPERVISOR,
        health=ComponentHealth.HEALTHY if healthy else ComponentHealth.BLOCKED,
        observed_at=now,
        state_ref=opaque_state_ref({"state_version": state_version}),
        reason_codes=() if healthy else ("risk_supervisor.reconciliation_loss",),
        correlation_id="paper-runtime-1",
    )
    return build_overview(
        {
            Component.RISK_SUPERVISOR: SourceRead(
                component=Component.RISK_SUPERVISOR,
                observation=observation,
            )
        },
        now=now,
        policy=_policy(),
    )


def test_current_view_updates_during_cooldown_without_rewriting_emitted_event() -> None:
    engine = AlertEngine(policy=_policy())
    first_overview = _overview(now=NOW, state_version="v1")
    first_event = engine.evaluate(first_overview)[0]
    first_event_hash = first_event.source_observation_hash
    first_event_id = first_event.event_id

    current = engine.active_alerts
    assert len(current) == 1
    assert current[0].alert_id == first_event.alert_id
    assert current[0].severity is AlertSeverity.CRITICAL
    assert current[0].next_action == "run_reconciliation_recovery"
    assert current[0].evaluated_at == NOW
    assert current[0].source_observation_hash == first_event_hash

    second_at = NOW + timedelta(seconds=10)
    second_overview = _overview(now=second_at, state_version="v2")
    assert engine.evaluate(second_overview) == ()

    refreshed = engine.active_alerts[0]
    assert refreshed.alert_id == first_event.alert_id
    assert refreshed.evaluated_at == second_at
    assert refreshed.source_observation_hash == second_overview.components[0].observation_hash
    assert refreshed.source_observation_hash != first_event_hash
    assert first_event.event_id == first_event_id
    assert first_event.source_observation_hash == first_event_hash

    payload = active_alert_mapping(refreshed)
    assert payload["alert_id"] == first_event.alert_id
    assert payload["severity"] == "critical"
    assert payload["next_action"] == "run_reconciliation_recovery"
    assert "event_id" not in payload
    assert "transition" not in payload


def test_reading_current_view_does_not_advance_cooldown() -> None:
    engine = AlertEngine(policy=_policy())
    engine.evaluate(_overview(now=NOW, state_version="v1"))

    assert engine.active_alerts == engine.active_alerts
    before_cooldown = _overview(now=NOW + timedelta(seconds=29), state_version="v2")
    assert engine.evaluate(before_cooldown) == ()
    reminder = engine.evaluate(_overview(now=NOW + timedelta(seconds=30), state_version="v3"))
    assert len(reminder) == 1
    assert reminder[0].alert_id == engine.active_alerts[0].alert_id


def test_time_travel_after_suppressed_update_is_rejected_without_mutation() -> None:
    engine = AlertEngine(policy=_policy())
    engine.evaluate(_overview(now=NOW, state_version="v1"))
    latest_at = NOW + timedelta(seconds=10)
    engine.evaluate(_overview(now=latest_at, state_version="v2"))
    before = engine.active_alerts

    with pytest.raises(ValueError, match="cannot move backwards"):
        engine.evaluate(_overview(now=NOW + timedelta(seconds=5), state_version="v3"))

    assert engine.active_alerts == before
    assert engine.active_alerts[0].evaluated_at == latest_at


def test_clear_removes_current_view() -> None:
    engine = AlertEngine(policy=_policy())
    engine.evaluate(_overview(now=NOW, state_version="v1"))
    assert engine.active_alerts

    events = engine.evaluate(
        _overview(now=NOW + timedelta(seconds=1), state_version="healthy", healthy=True)
    )

    assert len(events) == 1
    assert events[0].transition.value == "cleared"
    assert engine.active_alerts == ()
    assert engine.active_count == 0


def test_restart_reconstructs_same_condition_identity_from_authoritative_overview() -> None:
    first = AlertEngine(policy=_policy())
    first.evaluate(_overview(now=NOW, state_version="v1"))
    first_alert_id = first.active_alerts[0].alert_id

    restarted = AlertEngine(policy=_policy())
    restarted.evaluate(_overview(now=NOW + timedelta(seconds=1), state_version="v2"))

    assert restarted.active_alerts[0].alert_id == first_alert_id
    assert restarted.active_alerts[0].evaluated_at == NOW + timedelta(seconds=1)
