from __future__ import annotations

import json
from datetime import UTC, datetime

from fastapi import Request
from fastapi.testclient import TestClient

from modular_control_plane.api import create_api, reader_auth_required
from modular_control_plane.observability_service import build_observability_control
from modular_control_plane.service import ControlPlaneService
from modular_control_plane.system_api import create_system_resources_router
from modular_control_plane.system_resources import (
    ResourceUsage,
    SystemResourcesControlService,
    SystemResourcesSnapshot,
)

NOW = datetime(2026, 8, 28, 4, 45, tzinfo=UTC)
READER = "observability-reader-token"
PRIVATE_CANARY = "/Users/operator/private/TOP-SECRET-observability"


def fixed_clock() -> datetime:
    return NOW


class StaticResources:
    def snapshot(self) -> SystemResourcesSnapshot:
        return SystemResourcesSnapshot(
            collected_at=NOW,
            cpu_percent=10.0,
            logical_cpu_count=8,
            memory=ResourceUsage(
                total_bytes=1_000_000,
                used_bytes=400_000,
                available_bytes=600_000,
                percent=40.0,
            ),
            disk=ResourceUsage(
                total_bytes=2_000_000,
                used_bytes=500_000,
                available_bytes=1_500_000,
                percent=25.0,
            ),
            uptime_seconds=600.0,
            process_count=40,
            control_process_rss_bytes=12_000_000,
            control_process_threads=4,
        )


class ExplodingObservability:
    def snapshot(self):
        raise RuntimeError(PRIVATE_CANARY)


def authorize_reader(request: Request) -> None:
    if request.headers.get("Authorization") != f"Bearer {READER}":
        raise reader_auth_required()


def _client(*, attach: bool = True, exploding: bool = False) -> TestClient:
    service = ControlPlaneService(clock=fixed_clock)
    resources = SystemResourcesControlService(StaticResources())
    control = None
    if attach:
        control = (
            ExplodingObservability()
            if exploding
            else build_observability_control(service, resources, clock=fixed_clock)
        )
    application = create_api(service, authorize_reader=authorize_reader)
    application.include_router(
        create_system_resources_router(
            resources,
            observability_control=control,  # type: ignore[arg-type]
            attach_environment_capacity=False,
            authorize_reader=authorize_reader,
            clock=fixed_clock,
        )
    )
    return TestClient(application)


def test_observability_route_requires_reader_and_returns_strict_fail_closed_snapshot() -> None:
    client = _client()

    denied = client.get("/api/control/v1/observability/overview")
    accepted = client.get(
        "/api/control/v1/observability/overview",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "CONTROL_READER_AUTH_REQUIRED"
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    payload = accepted.json()
    assert payload["schema_version"] == 1
    assert payload["mode"] == "PAPER"
    assert payload["overview"]["overall_health"] == "unavailable"
    assert len(payload["overview"]["components"]) == 12
    assert {item["component"] for item in payload["overview"]["components"]} == {
        "market_data",
        "dataset_registry",
        "portfolio_position",
        "risk_sizing",
        "risk_supervisor",
        "trading_runner",
        "paper_execution",
        "backtest_research",
        "strategy_audit_factory",
        "signal_bot",
        "system_acceptance_recovery",
        "system_resources",
    }
    rendered = json.dumps(payload)
    assert PRIVATE_CANARY not in rendered
    assert "/Users/" not in rendered


def test_missing_observability_attachment_is_typed_fail_closed() -> None:
    response = _client(attach=False).get(
        "/api/control/v1/observability/overview",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    error = response.json()["error"]
    assert error["code"] == "CONTROL_OBSERVABILITY_NOT_ATTACHED"
    assert error["retryable"] is False
    assert error["technical_details"] == {}


def test_observability_failure_does_not_leak_exception_text_or_private_path() -> None:
    response = _client(exploding=True).get(
        "/api/control/v1/observability/overview",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "CONTROL_OBSERVABILITY_UNAVAILABLE"
    assert error["retryable"] is True
    assert error["technical_details"] == {"error_type": "RuntimeError"}
    rendered = json.dumps(error)
    assert PRIVATE_CANARY not in rendered
    assert "/Users/" not in rendered
