from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from typing import Protocol

from .contracts import ComponentHealth, ComponentId, HealthStatus, SafeScalar, SystemOverview


class ComponentAdapter(Protocol):
    """Lightweight operator-facing projection of one authoritative component."""

    @property
    def component(self) -> ComponentId: ...

    def health(self) -> ComponentHealth: ...


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(UTC)


class ControlPlaneService:
    """Aggregate component projections without importing component engines."""

    def __init__(
        self,
        adapters: Iterable[ComponentAdapter] = (),
        *,
        required_components: Iterable[ComponentId] = (),
        clock: Clock = utc_now,
    ) -> None:
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._clock = clock
        self._adapters: dict[ComponentId, ComponentAdapter] = {}
        for adapter in adapters:
            component = adapter.component
            if not isinstance(component, ComponentId):
                raise TypeError("adapter.component must be ComponentId")
            if component in self._adapters:
                raise ValueError(f"duplicate component adapter: {component.value}")
            self._adapters[component] = adapter

        required = tuple(required_components)
        if any(not isinstance(component, ComponentId) for component in required):
            raise TypeError("required_components must contain ComponentId values")
        if len(set(required)) != len(required):
            raise ValueError("required_components must be unique")
        self._required_components = frozenset(required)

    @property
    def registered_components(self) -> tuple[ComponentId, ...]:
        return tuple(sorted(self._adapters, key=str))

    def health(self, component: ComponentId) -> ComponentHealth:
        if not isinstance(component, ComponentId):
            raise TypeError("component must be ComponentId")
        adapter = self._adapters.get(component)
        if adapter is None:
            return self._unavailable(
                component,
                summary="Component adapter is not registered",
                reason="adapter_not_registered",
            )
        try:
            result = adapter.health()
        except Exception as exc:  # adapters are isolation boundaries
            return self._unavailable(
                component,
                summary="Component health check failed",
                reason="health_check_failed",
                error_type=type(exc).__name__,
            )
        if result.component is not component:
            return self._unavailable(
                component,
                summary="Component adapter returned a mismatched identity",
                reason="adapter_component_mismatch",
                reported_component=result.component.value,
            )
        return result

    def overview(self) -> SystemOverview:
        components = set(self._adapters) | set(self._required_components)
        snapshots = tuple(self.health(component) for component in sorted(components, key=str))
        return SystemOverview(
            status=self._aggregate_status(snapshots),
            checked_at=self._checked_at(),
            components=snapshots,
        )

    @staticmethod
    def _aggregate_status(components: tuple[ComponentHealth, ...]) -> HealthStatus:
        if not components:
            return HealthStatus.UNAVAILABLE
        statuses = {component.status for component in components}
        if statuses == {HealthStatus.UNAVAILABLE}:
            return HealthStatus.UNAVAILABLE
        if statuses == {HealthStatus.HEALTHY}:
            return HealthStatus.HEALTHY
        return HealthStatus.DEGRADED

    def _unavailable(
        self,
        component: ComponentId,
        *,
        summary: str,
        reason: str,
        error_type: str | None = None,
        reported_component: str | None = None,
    ) -> ComponentHealth:
        details: dict[str, SafeScalar] = {"reason": reason}
        if error_type is not None:
            details["error_type"] = error_type
        if reported_component is not None:
            details["reported_component"] = reported_component
        return ComponentHealth(
            component=component,
            status=HealthStatus.UNAVAILABLE,
            checked_at=self._checked_at(),
            summary=summary,
            details=details,
        )

    def _checked_at(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime):
            raise TypeError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("clock must return a timezone-aware datetime")
        return value
