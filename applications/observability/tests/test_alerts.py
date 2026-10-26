from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_observability import (
    AlertEngine,
    AlertSeverity,
    AlertTransition,
    Component,
    ComponentHealth,
    ObservabilityPolicy,
    SourceObservation,
    SourceRead,
    build_overview,
    opaque_state_ref,
    structured_alert_event,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _policy(component: Component = Component.RISK_SUPERVISOR) -> ObservabilityPolicy:
    return ObservabilityPolicy(
        freshness_seconds={component: 300.0},
        required_components=frozenset({component}),
        critical_components=frozenset({component}),
        alert_cooldown_seconds=30.0,
        alert_state_capacity=1,
    )


def _overview(
    *,
    now: datetime,
    component: Component = Component.RISK_SUPERVISOR,
    health: ComponentHealth = ComponentHealth.BLOCKED,
    reasons: tuple[str, ...] = ("risk_supervisor.reconciliation_loss",),
    state_version: str = "v1",
):
    observation = SourceObservation(
        component=component,
        health=health,
        observed_at=now,
        state_ref=opaque_state_ref({"component": component.value, "state_version": state_version}),
        reason_codes=reasons,
        correlation_id="paper-runtime-1",
    )
    return build_overview(
        {component: SourceRead(component=component, observation=observation)},
        now=now,
        policy=_policy(component),
    )


def test_alert_is_actionable_critical_and_structured() -> None:
    engine = AlertEngine(policy=_policy())

    events = engine.evaluate(_overview(now=NOW))

    assert len(events) == 1
    alert = events[0]
    assert alert.severity is AlertSeverity.CRITICAL
    assert alert.transition is AlertTransition.ACTIVE
    assert alert.next_action == "run_reconciliation_recovery"
    assert alert.correlation_id == "paper-runtime-1"
    structured = structured_alert_event(alert)
    assert structured["event_type"] == "observability.alert.active"
    assert structured["event_id"] == alert.event_id


def test_dedupe_cooldown_and_restart_idempotence() -> None:
    engine = AlertEngine(policy=_policy())
    first = engine.evaluate(_overview(now=NOW))[0]

    assert engine.evaluate(_overview(now=NOW + timedelta(seconds=29))) == ()
    reminder = engine.evaluate(_overview(now=NOW + timedelta(seconds=30)))[0]

    assert reminder.alert_id == first.alert_id
    assert reminder.event_id == first.event_id
    assert engine.active_count == 1

    restarted = AlertEngine(policy=_policy())
    replay = restarted.evaluate(_overview(now=NOW + timedelta(seconds=30)))[0]
    assert replay.alert_id == first.alert_id
    assert replay.event_id == first.event_id


def test_clear_semantics_remove_transient_active_state() -> None:
    engine = AlertEngine(policy=_policy())
    active = engine.evaluate(_overview(now=NOW))[0]
    healthy = _overview(
        now=NOW + timedelta(seconds=1),
        health=ComponentHealth.HEALTHY,
        reasons=(),
        state_version="v2",
    )

    events = engine.evaluate(healthy)

    assert len(events) == 1
    cleared = events[0]
    assert cleared.alert_id == active.alert_id
    assert cleared.transition is AlertTransition.CLEARED
    assert cleared.next_action == "verify_owner_recovered"
    assert engine.active_count == 0


def test_reason_change_clears_old_condition_and_activates_new_condition() -> None:
    engine = AlertEngine(policy=_policy())
    old = engine.evaluate(_overview(now=NOW))[0]

    events = engine.evaluate(
        _overview(
            now=NOW + timedelta(seconds=1),
            reasons=("risk_supervisor.resource_pressure",),
            state_version="v2",
        )
    )

    assert [event.transition for event in events] == [
        AlertTransition.CLEARED,
        AlertTransition.ACTIVE,
    ]
    assert events[0].alert_id == old.alert_id
    assert events[1].alert_id != old.alert_id
    assert events[1].next_action == "relieve_resource_pressure"


@pytest.mark.parametrize(
    ("reason", "next_action"),
    [
        ("risk_supervisor.rejection_rate_limit", "inspect_execution_rejections"),
        ("risk_supervisor.cancel_failure_rate_limit", "inspect_cancel_failures"),
        ("risk_supervisor.resource_pressure", "relieve_resource_pressure"),
        ("risk_supervisor.market_data_stale", "restore_market_data_freshness"),
        ("risk_supervisor.checkpoint_integrity_failure", "verify_checkpoint_recovery"),
    ],
)
def test_required_operator_reason_actions(reason: str, next_action: str) -> None:
    engine = AlertEngine(policy=_policy())

    alert = engine.evaluate(_overview(now=NOW, reasons=(reason,)))[0]

    assert alert.next_action == next_action


def test_alert_engine_rejects_time_travel_without_mutating_active_state() -> None:
    engine = AlertEngine(policy=_policy())
    first = engine.evaluate(_overview(now=NOW))[0]

    with pytest.raises(ValueError, match="cannot move backwards"):
        engine.evaluate(_overview(now=NOW - timedelta(seconds=1)))

    assert engine.active_alert_ids == (first.alert_id,)
