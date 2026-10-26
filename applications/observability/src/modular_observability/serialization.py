from __future__ import annotations

from typing import Any

from .alerts import ActiveAlertView
from .contracts import ComponentObservation, OperationalOverview, OperatorAlert


def component_mapping(observation: ComponentObservation) -> dict[str, Any]:
    return {
        "schema_version": observation.schema_version,
        "component": observation.component.value,
        "health": observation.health.value,
        "observed_at": observation.observed_at.isoformat() if observation.observed_at else None,
        "evaluated_at": observation.evaluated_at.isoformat(),
        "age_seconds": observation.age_seconds,
        "stale": observation.stale,
        "progress_at": observation.progress_at.isoformat() if observation.progress_at else None,
        "progress_age_seconds": observation.progress_age_seconds,
        "stuck": observation.stuck,
        "state_ref": observation.state_ref,
        "reason_codes": list(observation.reason_codes),
        "correlation_id": observation.correlation_id,
        "public_details": dict(observation.public_details),
        "observation_hash": observation.observation_hash,
    }


def overview_mapping(overview: OperationalOverview) -> dict[str, Any]:
    return {
        "schema_version": overview.schema_version,
        "evaluated_at": overview.evaluated_at.isoformat(),
        "overall_health": overview.overall_health.value,
        "overview_hash": overview.overview_hash,
        "components": [component_mapping(item) for item in overview.components],
    }


def active_alert_mapping(alert: ActiveAlertView) -> dict[str, Any]:
    return {
        "schema_version": alert.schema_version,
        "alert_id": alert.alert_id,
        "component": alert.component.value,
        "severity": alert.severity.value,
        "reason_codes": list(alert.reason_codes),
        "next_action": alert.next_action,
        "evaluated_at": alert.evaluated_at.isoformat(),
        "source_observation_hash": alert.source_observation_hash,
        "correlation_id": alert.correlation_id,
    }


def alert_mapping(alert: OperatorAlert) -> dict[str, Any]:
    return {
        "schema_version": alert.schema_version,
        "alert_id": alert.alert_id,
        "event_id": alert.event_id,
        "component": alert.component.value,
        "severity": alert.severity.value,
        "transition": alert.transition.value,
        "reason_codes": list(alert.reason_codes),
        "next_action": alert.next_action,
        "evaluated_at": alert.evaluated_at.isoformat(),
        "source_observation_hash": alert.source_observation_hash,
        "correlation_id": alert.correlation_id,
    }


def structured_alert_event(alert: OperatorAlert) -> dict[str, Any]:
    payload = alert_mapping(alert)
    payload["event_type"] = f"observability.alert.{alert.transition.value}"
    return payload
