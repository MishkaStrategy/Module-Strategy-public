from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import cast

from market_data_application.contracts import HealthResponse

from modular_control_plane import ComponentId, HealthStatus
from modular_control_plane.adapters.market_data import (
    MarketDataHealthAdapter,
    MarketDataHealthSnapshot,
    from_market_data_application,
)

NOW = datetime(2026, 8, 7, 4, 5, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


def snapshot(
    *,
    status: str = "ok",
    component: str = "market_data_api",
    api_version: str = "v1",
    checks: dict[str, str] | None = None,
) -> MarketDataHealthSnapshot:
    response = HealthResponse(
        status=cast("str", status),  # runtime test intentionally covers unknown source values below
        component=cast("str", component),
        api_version=cast("str", api_version),
        build_version="0.4.0",
        checks=checks or {"jobs": "ok", "registry": "ok", "worker": "private-worker-id"},
    )
    return cast(MarketDataHealthSnapshot, response)


def test_maps_existing_market_data_v1_health_contract() -> None:
    adapter = MarketDataHealthAdapter(lambda: snapshot(), clock=fixed_clock)
    health = adapter.health()

    assert health.component is ComponentId.MARKET_DATA
    assert health.status is HealthStatus.HEALTHY
    assert health.checked_at == NOW
    assert health.details == {
        "api_version": "v1",
        "build_version": "0.4.0",
        "jobs": "ok",
        "registry": "ok",
    }
    assert "worker" not in health.model_dump_json()
    assert "private-worker-id" not in health.model_dump_json()


def test_maps_degraded_and_error_without_conflating_health_axes() -> None:
    degraded = MarketDataHealthAdapter(
        lambda: snapshot(status="degraded", checks={"jobs": "ok", "registry": "warning"}),
        clock=fixed_clock,
    ).health()
    unavailable = MarketDataHealthAdapter(
        lambda: snapshot(status="error", checks={"jobs": "error:OSError", "registry": "ok"}),
        clock=fixed_clock,
    ).health()

    assert degraded.status is HealthStatus.DEGRADED
    assert unavailable.status is HealthStatus.UNAVAILABLE
    assert unavailable.details["jobs"] == "error:OSError"


def test_unknown_market_data_status_fails_closed() -> None:
    raw = SimpleNamespace(
        status="future-status",
        component="market_data_api",
        api_version="v1",
        build_version="0.5.0",
        checks={"jobs": "ok", "registry": "ok"},
    )
    adapter = MarketDataHealthAdapter(
        lambda: cast(MarketDataHealthSnapshot, raw),
        clock=fixed_clock,
    )
    health = adapter.health()

    assert health.status is HealthStatus.UNAVAILABLE
    assert health.details["reason"] == "unknown_market_data_health_status"
    assert health.details["source_status"] == "future-status"


def test_unknown_market_data_contract_version_fails_closed() -> None:
    raw = SimpleNamespace(
        status="ok",
        component="market_data_api",
        api_version="v2",
        build_version="0.5.0",
        checks={"jobs": "ok", "registry": "ok"},
    )
    adapter = MarketDataHealthAdapter(
        lambda: cast(MarketDataHealthSnapshot, raw),
        clock=fixed_clock,
    )
    health = adapter.health()

    assert health.status is HealthStatus.UNAVAILABLE
    assert health.details == {
        "api_version": "v2",
        "component": "market_data_api",
        "reason": "unsupported_market_data_contract",
    }


@dataclass
class _JobStore:
    def list_jobs(self, *, limit: int) -> tuple[()]:
        assert limit == 1
        return ()


@dataclass
class _Registry:
    def health(self) -> dict[str, str]:
        return {"status": "ok"}


@dataclass
class _Settings:
    instance_id: str = "must-not-leak-worker-id"


@dataclass
class _Application:
    context: object


def test_factory_reads_real_market_data_diagnostics_lazily() -> None:
    application = _Application(
        context=SimpleNamespace(
            job_store=_JobStore(),
            dataset_registry=_Registry(),
            settings=_Settings(),
        )
    )
    adapter = from_market_data_application(application, clock=fixed_clock)
    health = adapter.health()
    rendered = health.model_dump_json()

    assert health.status is HealthStatus.HEALTHY
    assert health.details["jobs"] == "ok"
    assert health.details["registry"] == "ok"
    assert "must-not-leak-worker-id" not in rendered
