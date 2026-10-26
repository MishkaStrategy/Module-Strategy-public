from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_observability import (
    AlertEngine,
    Component,
    ObservabilityPolicy,
    SourceRead,
    build_overview,
)

NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _policy() -> ObservabilityPolicy:
    return ObservabilityPolicy(
        freshness_seconds={Component.PAPER_EXECUTION: 30.0},
        required_components=frozenset({Component.PAPER_EXECUTION}),
        critical_components=frozenset({Component.PAPER_EXECUTION}),
        alert_cooldown_seconds=30.0,
        alert_state_capacity=1,
    )


def _unavailable(now: datetime):
    return build_overview(
        {
            Component.PAPER_EXECUTION: SourceRead(
                component=Component.PAPER_EXECUTION,
                observation=None,
                unavailable_reason="owner_unavailable",
            )
        },
        now=now,
        policy=_policy(),
    )


def test_unavailable_alert_identity_is_restart_idempotent_without_owner_state_ref() -> None:
    first = AlertEngine(policy=_policy()).evaluate(_unavailable(NOW))[0]
    replay = AlertEngine(policy=_policy()).evaluate(_unavailable(NOW + timedelta(seconds=1)))[0]

    assert replay.alert_id == first.alert_id
    assert replay.event_id == first.event_id
