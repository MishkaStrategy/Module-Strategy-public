from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from modular_control_plane import ControlPlaneService, PaperRuntimeControlService
from modular_control_plane.paper_api import create_paper_router
from modular_control_plane.server import ServerSettings, create_server

NOW = datetime(2026, 8, 7, 15, 45, tzinfo=UTC)


@dataclass(frozen=True)
class RuntimeSnapshot:
    runtime_id: str = "paper-runtime-1"
    portfolio_id: str = "portfolio-1"
    strategy_id: str = "strategy-accepted-1"
    native_state: str = "running"
    execution_mode: str = "paper"
    base_timeframe: str = "5m"
    market: str = "usdt_perpetual"
    instruments: tuple[str, ...] = ("BTCUSDT",)
    configuration_hash: str = "a" * 64
    kill_switch_active: bool = False
    kill_switch_scope: str = "global"
    kill_switch_activated_at: datetime | None = None
    kill_switch_cancellation_pending: bool = False
    health_ready: bool = True
    queue_depth: int | None = 0
    queue_capacity: int | None = 1024
    queue_high_water_mark: int | None = 3


@dataclass(frozen=True)
class ExecutionSnapshot:
    native_lifecycle: str = "ready"
    order_count: int = 1
    active_order_count: int = 1
    blocked_scope_count: int = 0


@dataclass(frozen=True)
class OrderSnapshot:
    client_order_id: str = "paper-entry-1"
    core_order_id: str = "core-order-1"
    portfolio_id: str = "portfolio-1"
    instrument: str = "BTCUSDT"
    intent_role: str = "entry"
    native_state: str = "open"
    requested_quantity: Decimal = Decimal("0.01")
    filled_quantity: Decimal = Decimal("0")
    remaining_quantity: Decimal = Decimal("0.01")
    average_fill_price: Decimal | None = None
    fees_paid: Decimal = Decimal("0")
    last_sequence: int = 0
    last_event_timestamp: datetime | None = None


@dataclass(frozen=True)
class ScopeSnapshot:
    portfolio_id: str
    instrument: str


class RuntimeReader:
    def snapshot(self) -> RuntimeSnapshot:
        return RuntimeSnapshot()


class ExecutionReader:
    def snapshot(self) -> ExecutionSnapshot:
        return ExecutionSnapshot()

    def list_orders(self, *, limit: int, offset: int) -> tuple[OrderSnapshot, ...]:
        rows = (OrderSnapshot(),)
        return rows[offset : offset + limit]

    def list_blocked_scopes(self) -> tuple[ScopeSnapshot, ...]:
        return ()


def _control() -> PaperRuntimeControlService:
    return PaperRuntimeControlService(RuntimeReader(), ExecutionReader(), clock=lambda: NOW)


def test_paper_runtime_api_preserves_reader_projection_and_operator_only_mutations() -> None:
    control = _control()
    app = create_server(
        ServerSettings(reader_token="reader-token", operator_token="operator-token"),
        service=ControlPlaneService([control], clock=lambda: NOW),
        paper_control=control,
    )
    client = TestClient(app)
    reader_headers = {"Authorization": "Bearer reader-token"}
    operator_headers = {"Authorization": "Bearer operator-token"}

    for path in (
        "/api/control/v1/paper/runtime",
        "/api/control/v1/paper/orders?limit=200&offset=0",
        "/api/control/v1/paper/blocked-scopes",
    ):
        assert client.get(path).status_code == 401
        response = client.get(path, headers=reader_headers)
        assert response.status_code == 200, response.text
        assert "private" not in response.text.lower()
        assert "reason" not in response.text.lower()
        assert "api_key" not in response.text.lower()

    runtime = client.get("/api/control/v1/paper/runtime", headers=reader_headers).json()
    assert runtime["execution_mode"] == "paper"
    assert runtime["native_state"] == "running"
    assert runtime["position_projection_available"] is False
    assert runtime["lifecycle_mutations_available"] is False

    order = client.get("/api/control/v1/paper/orders", headers=reader_headers).json()["orders"][0]
    assert order["client_order_id"] == "paper-entry-1"
    assert order["requested_quantity"] == "0.01"
    assert set(order) == {
        "schema_version",
        "client_order_id",
        "core_order_id",
        "portfolio_id",
        "instrument",
        "intent_role",
        "native_state",
        "requested_quantity",
        "filled_quantity",
        "remaining_quantity",
        "average_fill_price",
        "fees_paid",
        "last_sequence",
        "last_event_timestamp",
    }

    mutation_paths = (
        "/api/control/v1/paper/start",
        "/api/control/v1/paper/stop",
        "/api/control/v1/paper/kill-switch/activate",
        "/api/control/v1/paper/kill-switch/reset",
    )
    for path in mutation_paths:
        reader_denied = client.post(
            path,
            headers=reader_headers,
            json={"schema_version": 1, "request_id": "acceptance-owner-auth"},
        )
        assert reader_denied.status_code == 401
        assert reader_denied.json()["error"]["code"] == "CONTROL_OPERATOR_AUTH_REQUIRED"
        owner_unattached = client.post(
            path,
            headers=operator_headers,
            json={"schema_version": 1, "request_id": "acceptance-owner-auth"},
        )
        assert owner_unattached.status_code == 503
        assert owner_unattached.json()["error"]["code"] == "CONTROL_PAPER_LIFECYCLE_NOT_CONFIGURED"

    openapi = client.get("/api/control/v1/openapi.json", headers=reader_headers).json()
    expected_methods = {
        "/api/control/v1/paper/runtime": {"get"},
        "/api/control/v1/paper/orders": {"get"},
        "/api/control/v1/paper/blocked-scopes": {"get"},
        "/api/control/v1/paper/lifecycle-capability": {"get"},
        "/api/control/v1/paper/start": {"post"},
        "/api/control/v1/paper/stop": {"post"},
        "/api/control/v1/paper/kill-switch/activate": {"post"},
        "/api/control/v1/paper/kill-switch/reset": {"post"},
    }
    observed = {
        path: set(operations)
        for path, operations in openapi["paths"].items()
        if path.startswith("/api/control/v1/paper/")
    }
    assert observed == expected_methods


def test_unattached_paper_runtime_returns_typed_503_not_404() -> None:
    app = create_server(ServerSettings(), service=ControlPlaneService())
    response = TestClient(app).get("/api/control/v1/paper/runtime")

    assert response.status_code == 503
    payload = response.json()["error"]
    assert payload["code"] == "CONTROL_PAPER_RUNTIME_NOT_ATTACHED"
    assert payload["retryable"] is False


def test_paper_runtime_health_degrades_on_kill_switch_without_exposing_reason() -> None:
    @dataclass(frozen=True)
    class KillRuntime(RuntimeSnapshot):
        native_state: str = "kill_switch_active"
        kill_switch_active: bool = True
        kill_switch_activated_at: datetime | None = NOW

    class KillReader:
        def snapshot(self) -> KillRuntime:
            return KillRuntime()

    control = PaperRuntimeControlService(KillReader(), ExecutionReader(), clock=lambda: NOW)
    health = control.health()

    assert health.status.value == "DEGRADED"
    assert health.details["kill_switch_active"] is True
    assert "reason" not in repr(health).lower()


def test_standalone_router_accepts_explicit_reader_authorizer() -> None:
    control = _control()

    def authorize(request) -> None:
        if request.headers.get("authorization") != "Bearer reader-token":
            from fastapi import HTTPException

            raise HTTPException(status_code=401, detail="reader authentication required")

    router = create_paper_router(control, authorize_reader=authorize, clock=lambda: NOW)
    assert router.routes
