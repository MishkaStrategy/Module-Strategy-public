from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from importlib import import_module
from threading import Lock
from typing import Any, Protocol, cast

from .contracts import ComponentHealth, ComponentId, HealthStatus, SafeScalar
from .observability import (
    ActiveObservabilityAlert,
    ObservabilityAlertSeverity,
    ObservabilityComponentId,
    ObservabilityComponentView,
    ObservabilityHealth,
    UnifiedObservabilitySnapshot,
    UnifiedOperationalOverview,
    validate_backend_digest,
)
from .paper import (
    PaperExecutionNativeLifecycle,
    PaperRuntimeControlService,
    PaperRuntimeNativeState,
)
from .service import Clock, ControlPlaneService, utc_now
from .system_resources import SystemResourcesControlService, SystemResourcesSnapshot


class ObservabilityBackendUnavailable(RuntimeError):
    pass


class _BackendComponentView(Protocol):
    component: object
    health: object
    observed_at: datetime | None
    evaluated_at: datetime
    age_seconds: float | None
    stale: bool
    progress_at: datetime | None
    progress_age_seconds: float | None
    stuck: bool
    state_ref: str | None
    reason_codes: Sequence[str]
    correlation_id: str | None
    public_details: Mapping[str, SafeScalar]
    observation_hash: object


class _BackendOverview(Protocol):
    evaluated_at: datetime
    overall_health: object
    overview_hash: object
    components: Sequence[_BackendComponentView]


class _BackendCurrentAlert(Protocol):
    alert_id: object
    component: object
    severity: object
    reason_codes: Sequence[str]
    next_action: str
    evaluated_at: datetime
    source_observation_hash: object
    correlation_id: str | None


class _BackendService(Protocol):
    def snapshot(self, *, now: datetime) -> _BackendOverview: ...


class _BackendAlertEngine(Protocol):
    @property
    def active_alerts(self) -> tuple[_BackendCurrentAlert, ...]: ...

    def evaluate(self, overview: _BackendOverview) -> object: ...


class _BackendModule(Protocol):
    Component: Any
    ComponentHealth: Any
    UnavailableSource: Callable[..., object]
    CallableObservationSource: Callable[..., object]
    ObservabilityPolicy: Any
    ObservabilityService: Callable[..., _BackendService]
    AlertEngine: Callable[..., _BackendAlertEngine]
    SourceObservation: Callable[..., object]
    opaque_state_ref: Callable[[Mapping[str, object]], str]


class ObservabilityControlService:
    """Read-only Control Plane transport over the single backend observability projection."""

    def __init__(
        self,
        backend_service: object,
        alert_engine: object,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if not callable(getattr(backend_service, "snapshot", None)):
            raise TypeError("backend_service must implement snapshot(now=...)")
        if not callable(getattr(alert_engine, "evaluate", None)):
            raise TypeError("alert_engine must implement evaluate(overview)")
        if not hasattr(alert_engine, "active_alerts"):
            raise TypeError("alert_engine must expose active_alerts")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._backend_service = cast(_BackendService, backend_service)
        self._alert_engine = cast(_BackendAlertEngine, alert_engine)
        self._clock = clock
        self._lock = Lock()

    def snapshot(self) -> UnifiedObservabilitySnapshot:
        now = _aware_utc(self._clock(), "clock")
        with self._lock:
            overview = self._backend_service.snapshot(now=now)
            self._alert_engine.evaluate(overview)
            active_alerts = tuple(self._alert_engine.active_alerts)
            return _transport_snapshot(overview, active_alerts)


def build_observability_control(
    service: ControlPlaneService,
    resources: SystemResourcesControlService,
    *,
    paper_control: PaperRuntimeControlService | None = None,
    native_sources: Sequence[object] = (),
    clock: Clock = utc_now,
) -> ObservabilityControlService:
    """Compose complete owner observations; partial legacy health stays fail-closed."""

    if not isinstance(service, ControlPlaneService):
        raise TypeError("service must be ControlPlaneService")
    if not isinstance(resources, SystemResourcesControlService):
        raise TypeError("resources must be SystemResourcesControlService")
    backend = _load_backend()
    component_type = backend.Component
    unavailable_source = backend.UnavailableSource
    callable_source = backend.CallableObservationSource
    policy = backend.ObservabilityPolicy.paper_default()

    source_by_component: dict[str, object] = {}
    for source in native_sources:
        component = getattr(source, "component", None)
        component_value = _enum_text(component, "native source component")
        try:
            component_type(component_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"unsupported native observability component: {component_value}"
            ) from exc
        if component_value in source_by_component:
            raise ValueError(f"duplicate native observability source: {component_value}")
        if not callable(getattr(source, "read", None)):
            raise TypeError("native observability source must implement read(now=...)")
        source_by_component[component_value] = source

    # These projections use legacy contracts only where that contract contains the exact fact
    # represented here. A readable API/store is not equivalent to market freshness, stuck-work
    # detection, or strategy admission integrity, so those components require native #378 sources.
    derived: dict[str, Callable[..., object]] = {
        ObservabilityComponentId.DATASET_REGISTRY.value: (
            lambda *, now: _dataset_registry_observation(
                backend,
                service.health(ComponentId.MARKET_DATA),
            )
        ),
        ObservabilityComponentId.SIGNAL_BOT.value: lambda *, now: _legacy_component_observation(
            backend,
            component=ObservabilityComponentId.SIGNAL_BOT,
            health=service.health(ComponentId.SIGNAL_BOT),
        ),
        ObservabilityComponentId.SYSTEM_ACCEPTANCE_RECOVERY.value: (
            lambda *, now: _acceptance_recovery_observation(
                backend,
                service.health(ComponentId.ACCEPTANCE),
            )
        ),
        ObservabilityComponentId.SYSTEM_RESOURCES.value: lambda *, now: _resources_observation(
            backend,
            resources.resources(),
        ),
    }
    if paper_control is not None:
        derived[ObservabilityComponentId.TRADING_RUNNER.value] = lambda *, now: _runner_observation(
            backend, paper_control, now=now
        )
        derived[ObservabilityComponentId.PAPER_EXECUTION.value] = lambda *, now: (
            _paper_execution_observation(backend, paper_control, now=now)
        )

    sources: list[object] = []
    for component in component_type:
        component_value = _enum_text(component, "backend component")
        source = source_by_component.get(component_value)
        if source is not None:
            sources.append(source)
            continue
        query = derived.get(component_value)
        if query is None:
            sources.append(
                unavailable_source(component=component, unavailable_reason="owner_unavailable")
            )
        else:
            sources.append(callable_source(component=component, query=query))

    backend_service = backend.ObservabilityService(policy=policy, sources=sources)
    alert_engine = backend.AlertEngine(policy=policy)
    return ObservabilityControlService(backend_service, alert_engine, clock=clock)


def _load_backend() -> _BackendModule:
    try:
        return cast(_BackendModule, import_module("modular_observability"))
    except ModuleNotFoundError as exc:
        if exc.name == "modular_observability":
            raise ObservabilityBackendUnavailable(
                "modular_observability is not installed in this Control Plane environment"
            ) from exc
        raise


def _legacy_component_observation(
    backend: _BackendModule,
    *,
    component: ObservabilityComponentId,
    health: ComponentHealth,
) -> object:
    backend_health = _backend_health(backend, health.status)
    reasons = _legacy_reasons(((health.component, health.status),))
    details: dict[str, SafeScalar] = {
        "control_component": health.component.value,
        "control_status": health.status.value,
    }
    return _source_observation(
        backend,
        component=component,
        health=backend_health,
        observed_at=health.checked_at,
        reasons=reasons,
        state_payload={"health": _component_health_payload(health)},
        public_details=details,
    )


def _dataset_registry_observation(
    backend: _BackendModule,
    market_health: ComponentHealth,
) -> object:
    raw_status = market_health.details.get("registry")
    registry_status = raw_status if isinstance(raw_status, str) else "unknown"
    if registry_status == "ok":
        health = backend.ComponentHealth.HEALTHY
        reasons: tuple[str, ...] = ()
    elif registry_status == "degraded":
        health = backend.ComponentHealth.DEGRADED
        reasons = ("dataset_registry.degraded",)
    else:
        health = backend.ComponentHealth.UNAVAILABLE
        reasons = ("dataset_registry.unavailable",)
    return _source_observation(
        backend,
        component=ObservabilityComponentId.DATASET_REGISTRY,
        health=health,
        observed_at=market_health.checked_at,
        reasons=reasons,
        state_payload={
            "market_data_state": _component_health_payload(market_health),
            "registry_status": registry_status,
        },
        public_details={"registry_status": registry_status[:256]},
    )


def _acceptance_recovery_observation(
    backend: _BackendModule,
    acceptance: ComponentHealth,
) -> object:
    reasons = ["system_recovery.owner_unavailable"]
    if acceptance.status is not HealthStatus.HEALTHY:
        reasons.append(f"control_plane.acceptance.{acceptance.status.value.lower()}")
    return _source_observation(
        backend,
        component=ObservabilityComponentId.SYSTEM_ACCEPTANCE_RECOVERY,
        health=backend.ComponentHealth.UNAVAILABLE,
        observed_at=acceptance.checked_at,
        reasons=tuple(reasons),
        state_payload={
            "acceptance": _component_health_payload(acceptance),
            "recovery_attached": False,
        },
        public_details={
            "acceptance_status": acceptance.status.value,
            "recovery_attached": False,
        },
    )


def _resources_observation(
    backend: _BackendModule,
    resources: SystemResourcesSnapshot,
) -> object:
    return _source_observation(
        backend,
        component=ObservabilityComponentId.SYSTEM_RESOURCES,
        health=backend.ComponentHealth.HEALTHY,
        observed_at=resources.collected_at,
        reasons=(),
        state_payload={"resources": resources.model_dump(mode="json")},
        public_details={
            "cpu_percent": resources.cpu_percent,
            "memory_percent": resources.memory.percent,
            "disk_percent": resources.disk.percent,
            "process_count": resources.process_count,
            "control_process_threads": resources.control_process_threads,
        },
    )


def _runner_observation(
    backend: _BackendModule,
    paper: PaperRuntimeControlService,
    *,
    now: datetime,
) -> object:
    summary = paper.summary()
    reasons: list[str] = []
    if summary.kill_switch.active or summary.native_state in {
        PaperRuntimeNativeState.FAILED,
        PaperRuntimeNativeState.KILL_SWITCH_ACTIVE,
    }:
        health = backend.ComponentHealth.BLOCKED
    elif summary.native_state is PaperRuntimeNativeState.RUNNING and summary.health_ready:
        health = backend.ComponentHealth.HEALTHY
    else:
        health = backend.ComponentHealth.DEGRADED
    if health is not backend.ComponentHealth.HEALTHY:
        reasons.append(f"runner.{summary.native_state.value}")
    if summary.kill_switch.active:
        reasons.append("runner.kill_switch_active")
    if not summary.health_ready:
        reasons.append("runner.health_not_ready")
    public_details: dict[str, SafeScalar] = {
        "native_state": summary.native_state.value,
        "health_ready": summary.health_ready,
        "kill_switch_active": summary.kill_switch.active,
        "active_order_count": summary.active_order_count,
        "blocked_scope_count": summary.blocked_scope_count,
    }
    if summary.queue_depth is not None:
        public_details["queue_depth"] = summary.queue_depth
    if summary.queue_capacity is not None:
        public_details["queue_capacity"] = summary.queue_capacity
    return _source_observation(
        backend,
        component=ObservabilityComponentId.TRADING_RUNNER,
        health=health,
        observed_at=now,
        reasons=tuple(sorted(set(reasons))),
        state_payload={
            "runtime_id": summary.runtime_id,
            "configuration_hash": summary.configuration_hash,
            "native_state": summary.native_state.value,
            "health_ready": summary.health_ready,
            "kill_switch": summary.kill_switch.model_dump(mode="json"),
            "queue_depth": summary.queue_depth,
            "queue_capacity": summary.queue_capacity,
            "queue_high_water_mark": summary.queue_high_water_mark,
        },
        public_details=public_details,
    )


def _paper_execution_observation(
    backend: _BackendModule,
    paper: PaperRuntimeControlService,
    *,
    now: datetime,
) -> object:
    summary = paper.summary()
    lifecycle = summary.paper_execution_lifecycle
    reasons: list[str] = []
    if lifecycle in {
        PaperExecutionNativeLifecycle.BLOCKED,
        PaperExecutionNativeLifecycle.FAILED,
    }:
        health = backend.ComponentHealth.BLOCKED
    elif lifecycle is PaperExecutionNativeLifecycle.READY and summary.blocked_scope_count == 0:
        health = backend.ComponentHealth.HEALTHY
    else:
        health = backend.ComponentHealth.DEGRADED
    if health is not backend.ComponentHealth.HEALTHY:
        reasons.append(f"paper_execution.{lifecycle.value}")
    if summary.blocked_scope_count > 0:
        health = backend.ComponentHealth.BLOCKED
        reasons.append("paper_execution.reconciliation_blocked")
    return _source_observation(
        backend,
        component=ObservabilityComponentId.PAPER_EXECUTION,
        health=health,
        observed_at=now,
        reasons=tuple(sorted(set(reasons))),
        state_payload={
            "paper_execution_lifecycle": lifecycle.value,
            "order_count": summary.order_count,
            "active_order_count": summary.active_order_count,
            "blocked_scope_count": summary.blocked_scope_count,
        },
        public_details={
            "native_lifecycle": lifecycle.value,
            "order_count": summary.order_count,
            "active_order_count": summary.active_order_count,
            "blocked_scope_count": summary.blocked_scope_count,
        },
    )


def _source_observation(
    backend: _BackendModule,
    *,
    component: ObservabilityComponentId,
    health: object,
    observed_at: datetime,
    reasons: tuple[str, ...],
    state_payload: Mapping[str, object],
    public_details: Mapping[str, SafeScalar],
) -> object:
    component_type = backend.Component
    return backend.SourceObservation(
        component=component_type(component.value),
        health=health,
        observed_at=_aware_utc(observed_at, "observed_at"),
        state_ref=backend.opaque_state_ref(state_payload),
        reason_codes=reasons,
        public_details=dict(public_details),
    )


def _legacy_reasons(
    statuses: tuple[tuple[ComponentId, HealthStatus], ...],
) -> tuple[str, ...]:
    return tuple(
        sorted(
            f"control_plane.{identifier.value}.{status.value.lower()}"
            for identifier, status in statuses
            if status is not HealthStatus.HEALTHY
        )
    )


def _backend_health(backend: _BackendModule, status: HealthStatus) -> object:
    health_type = backend.ComponentHealth
    if status is HealthStatus.HEALTHY:
        return cast(object, health_type.HEALTHY)
    if status is HealthStatus.DEGRADED:
        return cast(object, health_type.DEGRADED)
    return cast(object, health_type.UNAVAILABLE)


def _component_health_payload(health: ComponentHealth) -> dict[str, object]:
    return {
        "component": health.component.value,
        "status": health.status.value,
        "checked_at": health.checked_at.astimezone(UTC).isoformat(),
        "summary": health.summary,
        "details": dict(health.details),
    }


def _transport_snapshot(
    overview: _BackendOverview,
    active_alerts: tuple[_BackendCurrentAlert, ...],
) -> UnifiedObservabilitySnapshot:
    evaluated_at = _aware_utc(overview.evaluated_at, "overview.evaluated_at")
    components = tuple(_transport_component(item) for item in overview.components)
    transport_overview = UnifiedOperationalOverview(
        evaluated_at=evaluated_at,
        overall_health=ObservabilityHealth(_enum_text(overview.overall_health, "overall_health")),
        overview_hash=validate_backend_digest(overview.overview_hash, "overview_hash"),
        components=components,
    )
    alerts = tuple(_transport_alert(item) for item in active_alerts)
    return UnifiedObservabilitySnapshot(overview=transport_overview, active_alerts=alerts)


def _transport_component(item: _BackendComponentView) -> ObservabilityComponentView:
    return ObservabilityComponentView(
        component=ObservabilityComponentId(_enum_text(item.component, "component")),
        health=ObservabilityHealth(_enum_text(item.health, "health")),
        observed_at=item.observed_at,
        evaluated_at=item.evaluated_at,
        age_seconds=item.age_seconds,
        stale=item.stale,
        progress_at=item.progress_at,
        progress_age_seconds=item.progress_age_seconds,
        stuck=item.stuck,
        state_ref=item.state_ref,
        reason_codes=tuple(item.reason_codes),
        correlation_id=item.correlation_id,
        public_details=dict(item.public_details),
        observation_hash=validate_backend_digest(item.observation_hash, "observation_hash"),
    )


def _transport_alert(item: _BackendCurrentAlert) -> ActiveObservabilityAlert:
    return ActiveObservabilityAlert(
        alert_id=validate_backend_digest(item.alert_id, "alert_id"),
        component=ObservabilityComponentId(_enum_text(item.component, "component")),
        severity=ObservabilityAlertSeverity(_enum_text(item.severity, "severity")),
        reason_codes=tuple(item.reason_codes),
        next_action=item.next_action,
        evaluated_at=item.evaluated_at,
        source_observation_hash=validate_backend_digest(
            item.source_observation_hash,
            "source_observation_hash",
        ),
        correlation_id=item.correlation_id,
    )


def _enum_text(value: object, name: str) -> str:
    candidate = getattr(value, "value", value)
    if not isinstance(candidate, str) or not candidate:
        raise ValueError(f"{name} must expose a non-empty string value")
    return candidate


def _aware_utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be a timezone-aware datetime")
    return value.astimezone(UTC)


__all__ = [
    "ObservabilityBackendUnavailable",
    "ObservabilityControlService",
    "build_observability_control",
]
