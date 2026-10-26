from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from .contracts import (
    Component,
    ComponentHealth,
    SourceObservation,
    SourceRead,
    correlation_id,
    opaque_state_ref,
    reason_code,
    state_ref,
    utc,
)


class ObservationSource(Protocol):
    @property
    def component(self) -> Component: ...

    def read(self, *, now: datetime) -> SourceRead: ...


class ObservationQuery(Protocol):
    def __call__(self, *, now: datetime) -> SourceObservation: ...


class RiskDecisionLike(Protocol):
    state: object
    evaluated_at: datetime
    guard_codes: tuple[object, ...]
    sequence: int
    snapshot_hash: str | None
    source_state_ids: Mapping[str, str]


class RiskSupervisorLike(Protocol):
    runtime_id: str

    def decision(self, *, now: datetime) -> RiskDecisionLike: ...


class RunnerConfigLike(Protocol):
    runtime_id: str


class KillSwitchLike(Protocol):
    active: bool


class RunnerLike(Protocol):
    state: object
    kill_switch: KillSwitchLike
    config: RunnerConfigLike


class HealthComponentLike(Protocol):
    name: str
    critical: bool
    status: object
    last_heartbeat: datetime | None
    consecutive_failures: int


class HealthSnapshotLike(Protocol):
    observed_at: datetime
    ready: bool
    live: bool
    components: Sequence[HealthComponentLike]


class HealthQueryLike(Protocol):
    def snapshot(self, *, now: datetime | None = None) -> HealthSnapshotLike: ...


def _enum_text(value: object, name: str) -> str:
    candidate = getattr(value, "value", value)
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(f"{name} must expose a non-empty string value")
    return candidate


def _public_identity(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be str")
    return correlation_id(value, name)


@dataclass(frozen=True, slots=True)
class UnavailableSource:
    component: Component
    unavailable_reason: str = "owner_unavailable"

    def __post_init__(self) -> None:
        if not isinstance(self.component, Component):
            raise TypeError("component must be Component")
        reason_code(self.unavailable_reason, "unavailable_reason")

    def read(self, *, now: datetime) -> SourceRead:
        utc(now, "now")
        return SourceRead(
            component=self.component,
            observation=None,
            unavailable_reason=self.unavailable_reason,
        )


@dataclass(frozen=True, slots=True)
class CallableObservationSource:
    component: Component
    query: ObservationQuery

    def __post_init__(self) -> None:
        if not isinstance(self.component, Component):
            raise TypeError("component must be Component")
        if not callable(self.query):
            raise TypeError("query must be callable")

    def read(self, *, now: datetime) -> SourceRead:
        moment = utc(now, "now")
        observation = self.query(now=moment)
        if not isinstance(observation, SourceObservation):
            raise TypeError("owner query must return SourceObservation")
        if observation.component is not self.component:
            raise ValueError("owner query returned a different component")
        return SourceRead(component=self.component, observation=observation)


@dataclass(frozen=True, slots=True)
class RiskSupervisorSource:
    owner: RiskSupervisorLike
    component: Component = Component.RISK_SUPERVISOR

    def read(self, *, now: datetime) -> SourceRead:
        moment = utc(now, "now")
        try:
            decision = self.owner.decision(now=moment)
        except Exception:
            return SourceRead(
                component=self.component,
                observation=None,
                unavailable_reason="owner_read_failed",
            )
        try:
            state = _enum_text(decision.state, "risk supervisor state")
            evaluated_at = utc(decision.evaluated_at, "risk supervisor evaluated_at")
            runtime_id = _public_identity(self.owner.runtime_id, "risk supervisor runtime_id")
            if isinstance(decision.sequence, bool) or not isinstance(decision.sequence, int):
                raise ValueError("risk supervisor sequence must be int")
            if decision.sequence < 0:
                raise ValueError("risk supervisor sequence must be non-negative")
            if decision.snapshot_hash is not None:
                state_ref(decision.snapshot_hash, "risk supervisor snapshot_hash")
            if not isinstance(decision.source_state_ids, Mapping):
                raise ValueError("risk supervisor source_state_ids must be a mapping")
            source_ids: dict[str, str] = {}
            for key, value in decision.source_state_ids.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("risk supervisor source_state_ids must contain strings")
                source_ids[key] = value
            guard_codes: list[str] = []
            for guard in decision.guard_codes:
                guard_value = _enum_text(guard, "risk supervisor guard code")
                guard_codes.append(reason_code(f"risk_supervisor.{guard_value}"))
            reasons = tuple(sorted(set(guard_codes)))
            if state == "healthy":
                if reasons:
                    raise ValueError("healthy risk decision cannot carry guard codes")
                health = ComponentHealth.HEALTHY
            elif state == "degraded":
                health = ComponentHealth.DEGRADED
                if not reasons:
                    reasons = ("risk_supervisor.degraded",)
            elif state == "halt":
                health = ComponentHealth.BLOCKED
                if not reasons:
                    reasons = ("risk_supervisor.halt",)
            elif state == "manual_review":
                health = ComponentHealth.BLOCKED
                reasons = tuple(sorted(set(reasons) | {"risk_supervisor.manual_review"}))
            else:
                raise ValueError("unknown risk supervisor state")
            owner_state_ref = opaque_state_ref(
                {
                    "sequence": decision.sequence,
                    "state": state,
                    "snapshot_hash": decision.snapshot_hash,
                    "guard_codes": list(reasons),
                    "source_state_ids": dict(sorted(source_ids.items())),
                }
            )
            observation = SourceObservation(
                component=self.component,
                health=health,
                observed_at=evaluated_at,
                state_ref=owner_state_ref,
                reason_codes=reasons,
                correlation_id=runtime_id,
                public_details={
                    "guard_count": len(reasons),
                    "sequence": decision.sequence,
                    "source_state_count": len(source_ids),
                    "state": state,
                },
            )
            return SourceRead(component=self.component, observation=observation)
        except (TypeError, ValueError):
            return SourceRead(
                component=self.component,
                observation=None,
                unavailable_reason="owner_state_invalid",
            )


@dataclass(frozen=True, slots=True)
class TradingRunnerSource:
    runner: RunnerLike
    health_query: HealthQueryLike
    component: Component = Component.TRADING_RUNNER

    def read(self, *, now: datetime) -> SourceRead:
        moment = utc(now, "now")
        try:
            snapshot = self.health_query.snapshot(now=moment)
        except Exception:
            return SourceRead(
                component=self.component,
                observation=None,
                unavailable_reason="owner_read_failed",
            )
        try:
            observed_at = utc(snapshot.observed_at, "runner health observed_at")
            runtime_id = _public_identity(self.runner.config.runtime_id, "runner runtime_id")
            state = _enum_text(self.runner.state, "runner state")
            kill_switch_active = self.runner.kill_switch.active
            if not isinstance(kill_switch_active, bool):
                raise ValueError("runner kill switch active must be bool")
            if not isinstance(snapshot.ready, bool) or not isinstance(snapshot.live, bool):
                raise ValueError("runner health ready/live must be bool")

            component_rows: list[tuple[str, bool, str, str | None, int]] = []
            reasons: set[str] = set()
            blocked = False
            degraded = False
            latest_heartbeat: datetime | None = None
            critical_component_count = 0
            total_consecutive_failures = 0
            for item in snapshot.components:
                if not isinstance(item.name, str) or not item.name:
                    raise ValueError("runner health component name must be non-empty")
                if not isinstance(item.critical, bool):
                    raise ValueError("runner health component critical must be bool")
                if isinstance(item.consecutive_failures, bool) or not isinstance(
                    item.consecutive_failures, int
                ):
                    raise ValueError("runner health consecutive_failures must be int")
                if item.consecutive_failures < 0:
                    raise ValueError("runner health consecutive_failures must be non-negative")
                if item.critical:
                    critical_component_count += 1
                total_consecutive_failures += item.consecutive_failures
                status = _enum_text(item.status, "runner health status")
                heartbeat: datetime | None = None
                if item.last_heartbeat is not None:
                    heartbeat = utc(item.last_heartbeat, "runner component last_heartbeat")
                    if latest_heartbeat is None or heartbeat > latest_heartbeat:
                        latest_heartbeat = heartbeat
                else:
                    degraded = True
                    reasons.add("runner.heartbeat_missing")
                if status == "critical":
                    reasons.add("runner.critical_health_failure")
                    if item.critical:
                        blocked = True
                    else:
                        degraded = True
                elif status in {"unhealthy", "degraded", "starting", "stopping", "stopped"}:
                    degraded = True
                    reasons.add("runner.component_degraded")
                elif status != "healthy":
                    raise ValueError("unknown runner health status")
                component_rows.append(
                    (
                        item.name,
                        item.critical,
                        status,
                        heartbeat.isoformat() if heartbeat is not None else None,
                        item.consecutive_failures,
                    )
                )

            if state in {"failed", "kill_switch_active"}:
                blocked = True
                reasons.add(f"runner.lifecycle_{state}")
            elif state == "degraded":
                degraded = True
                reasons.add("runner.lifecycle_degraded")
            elif state != "running":
                degraded = True
                reasons.add("runner.lifecycle_not_running")
            if kill_switch_active:
                blocked = True
                reasons.add("runner.kill_switch_active")
            if not snapshot.live:
                blocked = True
                reasons.add("runner.health_not_live")
            elif not snapshot.ready:
                degraded = True
                reasons.add("runner.health_not_ready")

            if blocked:
                health = ComponentHealth.BLOCKED
            elif degraded:
                health = ComponentHealth.DEGRADED
            else:
                health = ComponentHealth.HEALTHY
            reason_tuple = tuple(sorted(reasons))
            if health is not ComponentHealth.HEALTHY and not reason_tuple:
                reason_tuple = ("runner.degraded",)
            owner_state_ref = opaque_state_ref(
                {
                    "state": state,
                    "kill_switch_active": kill_switch_active,
                    "health_ready": snapshot.ready,
                    "health_live": snapshot.live,
                    "components": [list(row) for row in sorted(component_rows)],
                }
            )
            observation = SourceObservation(
                component=self.component,
                health=health,
                observed_at=observed_at,
                state_ref=owner_state_ref,
                reason_codes=reason_tuple,
                correlation_id=runtime_id,
                progress_at=latest_heartbeat,
                public_details={
                    "critical_component_count": critical_component_count,
                    "health_component_count": len(component_rows),
                    "health_live": snapshot.live,
                    "health_ready": snapshot.ready,
                    "kill_switch_active": kill_switch_active,
                    "state": state,
                    "total_consecutive_failures": total_consecutive_failures,
                },
            )
            return SourceRead(component=self.component, observation=observation)
        except (TypeError, ValueError):
            return SourceRead(
                component=self.component,
                observation=None,
                unavailable_reason="owner_state_invalid",
            )
