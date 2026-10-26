from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import import_module
from typing import Protocol, cast

from ..contracts import ComponentHealth, ComponentId, HealthStatus, SafeScalar
from ..service import Clock, utc_now


class MarketDataHealthSnapshot(Protocol):
    """Structural view of the existing Market Data v1 health contract."""

    @property
    def status(self) -> str: ...

    @property
    def component(self) -> str: ...

    @property
    def api_version(self) -> str: ...

    @property
    def build_version(self) -> str: ...

    @property
    def checks(self) -> Mapping[str, str]: ...


HealthReader = Callable[[], MarketDataHealthSnapshot]

_STATUS_MAP = {
    "ok": HealthStatus.HEALTHY,
    "degraded": HealthStatus.DEGRADED,
    "error": HealthStatus.UNAVAILABLE,
}
_ALLOWED_CHECKS = ("jobs", "registry")


class MarketDataHealthAdapter:
    """Project-facing projection of the authoritative Market Data health response."""

    component = ComponentId.MARKET_DATA

    def __init__(self, reader: HealthReader, *, clock: Clock = utc_now) -> None:
        if not callable(reader):
            raise TypeError("reader must be callable")
        if not callable(clock):
            raise TypeError("clock must be callable")
        self._reader = reader
        self._clock = clock

    def health(self) -> ComponentHealth:
        snapshot = self._reader()
        checked_at = self._checked_at()
        if snapshot.component != "market_data_api" or snapshot.api_version != "v1":
            return ComponentHealth(
                component=self.component,
                status=HealthStatus.UNAVAILABLE,
                checked_at=checked_at,
                summary="Market Data health contract is not compatible",
                details={
                    "reason": "unsupported_market_data_contract",
                    "component": _bounded(snapshot.component),
                    "api_version": _bounded(snapshot.api_version),
                },
            )

        normalized = snapshot.status.strip().lower()
        status = _STATUS_MAP.get(normalized)
        if status is None:
            return ComponentHealth(
                component=self.component,
                status=HealthStatus.UNAVAILABLE,
                checked_at=checked_at,
                summary="Market Data returned an unknown health status",
                details={
                    "reason": "unknown_market_data_health_status",
                    "source_status": _bounded(snapshot.status),
                    "api_version": snapshot.api_version,
                    "build_version": _bounded(snapshot.build_version),
                },
            )

        details: dict[str, SafeScalar] = {
            "api_version": snapshot.api_version,
            "build_version": _bounded(snapshot.build_version),
        }
        for name in _ALLOWED_CHECKS:
            value = snapshot.checks.get(name)
            if value is not None:
                details[name] = _bounded(value)

        summary = {
            HealthStatus.HEALTHY: "Market Data is healthy",
            HealthStatus.DEGRADED: "Market Data is degraded",
            HealthStatus.UNAVAILABLE: "Market Data is unavailable",
        }[status]
        return ComponentHealth(
            component=self.component,
            status=status,
            checked_at=checked_at,
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


def from_market_data_application(
    application: object,
    *,
    clock: Clock = utc_now,
) -> MarketDataHealthAdapter:
    """Create an adapter against the existing in-process Market Data application.

    The dependency is loaded lazily so importing the base Control Plane never initializes
    Market Data or its execution stack. Operational composition can share the same
    authoritative MarketDataApplication instance used by the existing API.
    """

    module = import_module("market_data_application.diagnostics")
    raw_reader = getattr(module, "health_response", None)
    if not callable(raw_reader):
        raise RuntimeError("market_data_application.diagnostics.health_response is unavailable")
    health_response = cast(Callable[[object], MarketDataHealthSnapshot], raw_reader)
    return MarketDataHealthAdapter(lambda: health_response(application), clock=clock)


def _bounded(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        return "unknown"
    return normalized[:1000]
