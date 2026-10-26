from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from .contracts import (
    MAX_REASON_CODES,
    SCHEMA_VERSION,
    AlertSeverity,
    AlertTransition,
    Component,
    ComponentHealth,
    ComponentObservation,
    ObservabilityPolicy,
    OperationalOverview,
    OperatorAlert,
    canonical_hash,
    correlation_id,
    reason_code,
    state_ref,
    utc,
)


@dataclass(frozen=True, slots=True)
class ActiveAlertView:
    """Current read-only alert condition, separate from immutable transition events."""

    alert_id: str
    component: Component
    severity: AlertSeverity
    reason_codes: tuple[str, ...]
    next_action: str
    evaluated_at: datetime
    source_observation_hash: str
    correlation_id: str | None = None
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise ValueError("unsupported active alert schema_version")
        state_ref(self.alert_id, "alert_id")
        state_ref(self.source_observation_hash, "source_observation_hash")
        if not isinstance(self.component, Component):
            raise TypeError("component must be Component")
        if not isinstance(self.severity, AlertSeverity):
            raise TypeError("severity must be AlertSeverity")
        if len(self.reason_codes) > MAX_REASON_CODES:
            raise ValueError("reason_codes exceeds bounded item limit")
        reasons = tuple(reason_code(value, "reason_codes item") for value in self.reason_codes)
        if not reasons:
            raise ValueError("active alert requires reason_codes")
        if len(set(reasons)) != len(reasons):
            raise ValueError("reason_codes cannot contain duplicates")
        object.__setattr__(self, "reason_codes", tuple(sorted(reasons)))
        reason_code(self.next_action, "next_action")
        object.__setattr__(self, "evaluated_at", utc(self.evaluated_at, "evaluated_at"))
        if self.correlation_id is not None:
            correlation_id(self.correlation_id)


@dataclass(slots=True)
class _ActiveAlert:
    alert_id: str
    component: Component
    severity: AlertSeverity
    reason_codes: tuple[str, ...]
    next_action: str
    source_state_ref: str | None
    source_observation_hash: str
    correlation_id: str | None
    last_evaluated_at: datetime
    last_emitted_at: datetime


def _condition_id(observation: ComponentObservation) -> str:
    return canonical_hash(
        {
            "schema_version": observation.schema_version,
            "component": observation.component.value,
            "health": observation.health.value,
            "reason_codes": list(observation.reason_codes),
        }
    )


def _event_id(
    *,
    alert_id: str,
    transition: AlertTransition,
    source_state_ref: str | None,
    source_observation_hash: str,
) -> str:
    del source_observation_hash
    return canonical_hash(
        {
            "alert_id": alert_id,
            "transition": transition.value,
            "source_state_ref": source_state_ref,
        }
    )


def _severity(
    observation: ComponentObservation,
    policy: ObservabilityPolicy,
) -> AlertSeverity:
    if observation.health in {ComponentHealth.BLOCKED, ComponentHealth.UNAVAILABLE}:
        return AlertSeverity.CRITICAL
    if observation.component in policy.critical_components:
        return AlertSeverity.CRITICAL
    return AlertSeverity.WARNING


def _next_action(observation: ComponentObservation) -> str:
    reasons = observation.reason_codes
    if any(reason.endswith("reconciliation_loss") for reason in reasons):
        return "run_reconciliation_recovery"
    if any(reason.endswith("reconciliation_blocked") for reason in reasons):
        return "resolve_reconciliation_block"
    if any(reason.endswith("rejection_rate_limit") for reason in reasons):
        return "inspect_execution_rejections"
    if any(reason.endswith("cancel_failure_rate_limit") for reason in reasons):
        return "inspect_cancel_failures"
    if any(reason.endswith("resource_pressure") for reason in reasons):
        return "relieve_resource_pressure"
    if any(reason.endswith("checkpoint_integrity_failure") for reason in reasons):
        return "verify_checkpoint_recovery"
    if any(reason.endswith("market_data_stale") for reason in reasons) or (
        observation.component is Component.MARKET_DATA and "source_stale" in reasons
    ):
        return "restore_market_data_freshness"
    if "source_progress_stalled" in reasons:
        return "inspect_stuck_owner_work"
    if any(reason.startswith("runner.") for reason in reasons):
        return "inspect_runner_health"
    if any(reason.startswith("risk_supervisor.") for reason in reasons):
        return "follow_risk_supervisor_recovery"
    if any(reason.startswith("owner_") for reason in reasons):
        return "restore_owner_visibility"
    if observation.health is ComponentHealth.BLOCKED:
        return "follow_owner_recovery"
    if observation.health is ComponentHealth.UNAVAILABLE:
        return "restore_owner_visibility"
    return "inspect_owner_degradation"


class AlertEngine:
    """Bounded transient dedupe/cooldown projection; never an execution authority."""

    def __init__(self, *, policy: ObservabilityPolicy) -> None:
        if not isinstance(policy, ObservabilityPolicy):
            raise TypeError("policy must be ObservabilityPolicy")
        self.policy = policy
        self._active: dict[Component, _ActiveAlert] = {}

    @property
    def active_alert_ids(self) -> tuple[str, ...]:
        return tuple(sorted(record.alert_id for record in self._active.values()))

    @property
    def active_alerts(self) -> tuple[ActiveAlertView, ...]:
        """Return the current bounded alert view without mutating cooldown state."""

        return tuple(
            self._active_view(record)
            for record in sorted(self._active.values(), key=lambda item: item.component.value)
        )

    @property
    def active_count(self) -> int:
        return len(self._active)

    def evaluate(self, overview: OperationalOverview) -> tuple[OperatorAlert, ...]:
        if not isinstance(overview, OperationalOverview):
            raise TypeError("overview must be OperationalOverview")
        component_set = frozenset(item.component for item in overview.components)
        if component_set != self.policy.required_components:
            raise ValueError("overview must contain the exact required component set")
        if len(overview.components) > self.policy.alert_state_capacity:
            raise ValueError("overview exceeds bounded alert state capacity")
        if any(
            record.last_evaluated_at > overview.evaluated_at for record in self._active.values()
        ):
            raise ValueError("alert evaluation time cannot move backwards")

        events: list[OperatorAlert] = []
        for observation in overview.components:
            prior = self._active.get(observation.component)
            if observation.health is ComponentHealth.HEALTHY:
                if prior is not None:
                    events.append(self._clear_event(prior, observation))
                    del self._active[observation.component]
                continue

            alert_id = _condition_id(observation)
            severity = _severity(observation, self.policy)
            next_action = _next_action(observation)
            if prior is None:
                record = self._record(
                    alert_id=alert_id,
                    observation=observation,
                    severity=severity,
                    next_action=next_action,
                )
                self._active[observation.component] = record
                events.append(self._active_event(record))
                continue

            if prior.alert_id != alert_id:
                events.append(self._clear_event(prior, observation))
                record = self._record(
                    alert_id=alert_id,
                    observation=observation,
                    severity=severity,
                    next_action=next_action,
                )
                self._active[observation.component] = record
                events.append(self._active_event(record))
                continue

            elapsed = (overview.evaluated_at - prior.last_emitted_at).total_seconds()
            prior.source_state_ref = observation.state_ref
            prior.source_observation_hash = observation.observation_hash
            prior.correlation_id = observation.correlation_id
            prior.severity = severity
            prior.next_action = next_action
            prior.last_evaluated_at = overview.evaluated_at
            if elapsed >= self.policy.alert_cooldown_seconds:
                prior.last_emitted_at = overview.evaluated_at
                events.append(self._active_event(prior))

        if len(self._active) > self.policy.alert_state_capacity:
            raise AssertionError("bounded alert state capacity invariant violated")
        return tuple(events)

    def _record(
        self,
        *,
        alert_id: str,
        observation: ComponentObservation,
        severity: AlertSeverity,
        next_action: str,
    ) -> _ActiveAlert:
        return _ActiveAlert(
            alert_id=alert_id,
            component=observation.component,
            severity=severity,
            reason_codes=observation.reason_codes,
            next_action=next_action,
            source_state_ref=observation.state_ref,
            source_observation_hash=observation.observation_hash,
            correlation_id=observation.correlation_id,
            last_evaluated_at=observation.evaluated_at,
            last_emitted_at=observation.evaluated_at,
        )

    @staticmethod
    def _active_view(record: _ActiveAlert) -> ActiveAlertView:
        return ActiveAlertView(
            alert_id=record.alert_id,
            component=record.component,
            severity=record.severity,
            reason_codes=record.reason_codes,
            next_action=record.next_action,
            evaluated_at=record.last_evaluated_at,
            source_observation_hash=record.source_observation_hash,
            correlation_id=record.correlation_id,
        )

    @staticmethod
    def _active_event(record: _ActiveAlert) -> OperatorAlert:
        return OperatorAlert(
            alert_id=record.alert_id,
            event_id=_event_id(
                alert_id=record.alert_id,
                transition=AlertTransition.ACTIVE,
                source_state_ref=record.source_state_ref,
                source_observation_hash=record.source_observation_hash,
            ),
            component=record.component,
            severity=record.severity,
            transition=AlertTransition.ACTIVE,
            reason_codes=record.reason_codes,
            next_action=record.next_action,
            evaluated_at=record.last_emitted_at,
            source_observation_hash=record.source_observation_hash,
            correlation_id=record.correlation_id,
        )

    @staticmethod
    def _clear_event(
        record: _ActiveAlert,
        observation: ComponentObservation,
    ) -> OperatorAlert:
        return OperatorAlert(
            alert_id=record.alert_id,
            event_id=_event_id(
                alert_id=record.alert_id,
                transition=AlertTransition.CLEARED,
                source_state_ref=observation.state_ref,
                source_observation_hash=observation.observation_hash,
            ),
            component=record.component,
            severity=record.severity,
            transition=AlertTransition.CLEARED,
            reason_codes=record.reason_codes,
            next_action="verify_owner_recovered",
            evaluated_at=observation.evaluated_at,
            source_observation_hash=observation.observation_hash,
            correlation_id=observation.correlation_id or record.correlation_id,
        )
