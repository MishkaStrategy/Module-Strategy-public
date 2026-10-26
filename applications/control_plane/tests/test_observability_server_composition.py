from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

import modular_control_plane.server as server_module
from modular_control_plane.contracts import ComponentId
from modular_control_plane.observability_service import ObservabilityBackendUnavailable
from modular_control_plane.server import ServerSettings, create_server
from modular_control_plane.service import ControlPlaneService
from modular_control_plane.system_resources import (
    ResourceUsage,
    SystemResourcesControlService,
    SystemResourcesSnapshot,
)

NOW = datetime(2026, 8, 28, 6, 30, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


class StaticResources:
    def snapshot(self) -> SystemResourcesSnapshot:
        return SystemResourcesSnapshot(
            collected_at=datetime.now(UTC),
            cpu_percent=10.0,
            logical_cpu_count=4,
            memory=ResourceUsage(
                total_bytes=1_000_000,
                used_bytes=250_000,
                available_bytes=750_000,
                percent=25.0,
            ),
            disk=ResourceUsage(
                total_bytes=2_000_000,
                used_bytes=500_000,
                available_bytes=1_500_000,
                percent=25.0,
            ),
            uptime_seconds=900.0,
            process_count=20,
            control_process_rss_bytes=10_000_000,
            control_process_threads=4,
        )


def _service() -> ControlPlaneService:
    return ControlPlaneService(
        (),
        required_components=tuple(ComponentId),
        clock=fixed_clock,
    )


def _resources() -> SystemResourcesControlService:
    return SystemResourcesControlService(StaticResources())


def test_standard_server_attaches_installed_observability_backend() -> None:
    application = create_server(
        ServerSettings(),
        service=_service(),
        system_resources_control=_resources(),
    )

    response = TestClient(application).get("/api/control/v1/observability/overview")

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "PAPER"
    assert payload["overview"]["overall_health"] == "unavailable"
    assert len(payload["overview"]["components"]) == 12
    component_health = {
        item["component"]: item["health"] for item in payload["overview"]["components"]
    }
    assert component_health["system_resources"] == "healthy"
    assert component_health["risk_supervisor"] == "unavailable"
    assert component_health["portfolio_position"] == "unavailable"


def test_standard_server_starts_without_optional_observability_backend(monkeypatch) -> None:
    def unavailable(*args, **kwargs):
        raise ObservabilityBackendUnavailable("backend intentionally absent")

    monkeypatch.setattr(server_module, "build_observability_control", unavailable)
    application = create_server(
        ServerSettings(),
        service=_service(),
        system_resources_control=_resources(),
    )

    response = TestClient(application).get("/api/control/v1/observability/overview")

    assert response.status_code == 503
    payload = response.json()
    assert payload["error"]["code"] == "CONTROL_OBSERVABILITY_NOT_ATTACHED"
    assert payload["error"]["retryable"] is False
