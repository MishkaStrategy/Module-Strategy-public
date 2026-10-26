from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from fastapi import HTTPException, Request
from fastapi.testclient import TestClient

from modular_control_plane import ComponentId, ControlPlaneService, HealthStatus
from modular_control_plane.adapters.market_data import MarketDataHealthAdapter
from modular_control_plane.adapters.market_data_http import (
    MarketDataHttpCatalogError,
    MarketDataHttpCatalogReader,
    MarketDataHttpHealthError,
    MarketDataHttpHealthReader,
    validate_market_data_base_url,
)
from modular_control_plane.adapters.signal_bot_http import (
    SignalBotHealthAdapter,
    SignalBotHttpHealthReader,
    validate_signal_bot_base_url,
)
from modular_control_plane.adapters.system_resources_psutil import PsutilSystemResourcesProvider
from modular_control_plane.api import create_api
from modular_control_plane.cli import main as cli_main
from modular_control_plane.client import ClientSettings, ControlClient
from modular_control_plane.market_data import MarketDatasetList
from modular_control_plane.system_api import create_system_resources_router
from modular_control_plane.system_resources import (
    ResourceUsage,
    SystemResourcesControlService,
    SystemResourcesSnapshot,
)

NOW = datetime(2026, 8, 7, 4, 20, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


def _healthy_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "ok",
        "component": "market_data_api",
        "api_version": "v1",
        "build_version": "0.4.0",
        "checks": {
            "jobs": "ok",
            "registry": "ok",
            "worker": "must-not-propagate",
        },
    }


def _dataset_payload(dataset_id: str = "btc-main", version: int = 3) -> dict[str, object]:
    return {
        "schema_version": 1,
        "dataset_id": dataset_id,
        "version": version,
        "dataset_hash": "a" * 64,
        "exchange_id": "binance",
        "market_type": "usdtm_futures",
        "instrument_id": "BTCUSDT",
        "data_types": ["klines"],
        "timeframes": ["5m", "15m"],
        "start": "2019-09-08T17:55:00+00:00",
        "end": "2026-07-19T06:55:00+00:00",
        "dataset_schema_version": 1,
        "validation_status": "VALID",
        "manifest_uri": "/private/market-data/manifest.json",
        "storage_uri": "/private/market-data/dataset",
        "unresolved_issues": ["private issue /Users/operator/data"],
        "lineage": ["/private/lineage/source-a"],
        "compatible_consumers": ["backtest-engine"],
        "descriptor_version": 1,
        "registered_at": "2026-08-01T01:02:03+00:00",
    }


def _system_snapshot() -> SystemResourcesSnapshot:
    return SystemResourcesSnapshot(
        collected_at=NOW,
        cpu_percent=12.5,
        logical_cpu_count=8,
        memory=ResourceUsage(
            total_bytes=16_000,
            used_bytes=6_000,
            available_bytes=10_000,
            percent=37.5,
        ),
        disk=ResourceUsage(
            total_bytes=100_000,
            used_bytes=25_000,
            available_bytes=75_000,
            percent=25.0,
        ),
        uptime_seconds=3600.0,
        process_count=42,
        control_process_rss_bytes=123_456,
        control_process_threads=7,
    )


def test_http_reader_calls_only_fixed_loopback_health_path_and_keeps_token_server_side() -> None:
    token = "reader-secret-token"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:8000/api/v1/health"
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(200, json=_healthy_payload())

    reader = MarketDataHttpHealthReader(
        "http://127.0.0.1:8000",
        token=token,
        transport=httpx.MockTransport(handler),
    )
    adapter = MarketDataHealthAdapter(reader, clock=fixed_clock)
    health = adapter.health()
    rendered = health.model_dump_json()

    assert health.status is HealthStatus.HEALTHY
    assert health.details["jobs"] == "ok"
    assert health.details["registry"] == "ok"
    assert token not in repr(reader)
    assert token not in rendered
    assert "must-not-propagate" not in rendered


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8000",
        "http://192.168.1.10:8000",
        "http://example.com:8000",
        "http://user:pass@127.0.0.1:8000",
        "http://127.0.0.1:8000/prefix",
        "http://127.0.0.1:8000?token=secret",
    ],
)
def test_market_data_url_rejects_nonlocal_or_ambiguous_targets(url: str) -> None:
    with pytest.raises(ValueError):
        validate_market_data_base_url(url)


def test_market_data_url_normalizes_supported_loopback_forms() -> None:
    assert validate_market_data_base_url(" http://localhost:8000/ ") == "http://localhost:8000"
    assert validate_market_data_base_url("http://[::1]:8000") == "http://[::1]:8000"


def test_non_200_response_body_is_never_exposed_through_control_plane() -> None:
    secret = "exchange-private-secret"

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text=secret)

    reader = MarketDataHttpHealthReader(
        "http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    service = ControlPlaneService(
        [MarketDataHealthAdapter(reader, clock=fixed_clock)],
        clock=fixed_clock,
    )
    health = service.health(ComponentId.MARKET_DATA)
    rendered = health.model_dump_json()

    assert health.status is HealthStatus.UNAVAILABLE
    assert health.details == {
        "error_type": "MarketDataHttpHealthError",
        "reason": "health_check_failed",
    }
    assert secret not in rendered


def test_oversized_health_payload_is_rejected_before_json_parsing() -> None:
    body = b"{" + b"x" * (64 * 1024) + b"}"

    reader = MarketDataHttpHealthReader(
        "http://localhost:8000",
        transport=httpx.MockTransport(lambda _request: httpx.Response(200, content=body)),
    )

    with pytest.raises(MarketDataHttpHealthError, match="exceeds 64 KiB"):
        reader()


def test_malformed_health_contract_fails_with_safe_error_type() -> None:
    reader = MarketDataHttpHealthReader(
        "http://localhost:8000",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={"status": "ok", "secret": "never"})
        ),
    )

    with pytest.raises(MarketDataHttpHealthError, match="contract validation failed") as captured:
        reader()
    assert "never" not in str(captured.value)


def test_catalog_list_projects_identity_but_never_native_uris_or_issue_text() -> None:
    token = "native-market-reader"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:8000/api/v1/datasets?limit=10&offset=2"
        assert request.headers["authorization"] == f"Bearer {token}"
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "items": [_dataset_payload()],
                "pagination": {"schema_version": 1, "limit": 10, "offset": 2, "total": 3},
            },
        )

    reader = MarketDataHttpCatalogReader(
        "http://127.0.0.1:8000",
        token=token,
        transport=httpx.MockTransport(handler),
    )
    page = reader.list_datasets(limit=10, offset=2)
    rendered = page.model_dump_json()

    assert page.items[0].dataset_id == "btc-main"
    assert page.items[0].unresolved_issue_count == 1
    assert page.items[0].lineage_count == 1
    assert page.total == 3
    assert token not in repr(reader)
    assert token not in rendered
    assert "/private/market-data" not in rendered
    assert "/Users/operator" not in rendered
    assert "/private/lineage" not in rendered
    assert "manifest_uri" not in rendered
    assert "storage_uri" not in rendered
    assert "unresolved_issues" not in rendered
    assert 'lineage"' not in rendered


def test_catalog_validation_projects_counts_not_manifest_or_raw_issue_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/datasets/btc-main/validation"
        assert request.url.params["version"] == "3"
        assert request.url.params["deep"] == "true"
        return httpx.Response(
            200,
            json={
                "schema_version": 1,
                "dataset_id": "btc-main",
                "version": 3,
                "dataset_hash": "b" * 64,
                "validation_status": "INVALID",
                "status": "error",
                "deep": True,
                "partitions_checked": 83,
                "raw_artifacts_checked": 83,
                "rows_checked": 721_597,
                "issues": [
                    "missing /private/month-1",
                    "checksum mismatch /Users/operator/raw.zip",
                ],
                "manifest_uri": "/private/manifest.json",
            },
        )

    reader = MarketDataHttpCatalogReader(
        "http://127.0.0.1:8000",
        transport=httpx.MockTransport(handler),
    )
    result = reader.validate_dataset("btc-main", version=3, deep=True)
    rendered = result.model_dump_json()

    assert result.issue_count == 2
    assert result.rows_checked == 721_597
    assert "missing /private" not in rendered
    assert "/Users/operator" not in rendered
    assert "manifest_uri" not in rendered
    assert "issues" not in rendered


def test_catalog_get_rejects_native_identity_mismatch() -> None:
    reader = MarketDataHttpCatalogReader(
        "http://127.0.0.1:8000",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json=_dataset_payload("other-dataset", 3))
        ),
    )

    with pytest.raises(MarketDataHttpCatalogError, match="identity mismatch"):
        reader.get_dataset("btc-main", version=3)


def test_catalog_preserves_only_upstream_status_not_error_body() -> None:
    secret = "native stack /private/path token=should-not-leak"
    reader = MarketDataHttpCatalogReader(
        "http://127.0.0.1:8000",
        transport=httpx.MockTransport(lambda _request: httpx.Response(404, text=secret)),
    )

    with pytest.raises(MarketDataHttpCatalogError) as captured:
        reader.get_dataset("missing", version=None)
    assert captured.value.status_code == 404
    assert secret not in str(captured.value)


def test_catalog_rejects_pagination_identity_mismatch() -> None:
    reader = MarketDataHttpCatalogReader(
        "http://127.0.0.1:8000",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "items": [],
                    "pagination": {"limit": 9, "offset": 0, "total": 0},
                },
            )
        ),
    )

    with pytest.raises(MarketDataHttpCatalogError, match="pagination identity mismatch"):
        reader.list_datasets(limit=10, offset=0)


@pytest.mark.parametrize(
    ("field", "unsafe_value", "secret"),
    [
        ("exchange_id", "/private/exchange", "/private/exchange"),
        ("instrument_id", "BTC\\private", "BTC\\private"),
        ("compatible_consumers", ["/private/consumer"], "/private/consumer"),
    ],
)
def test_catalog_rejects_path_like_projected_identity_without_echo(
    field: str,
    unsafe_value: object,
    secret: str,
) -> None:
    payload = _dataset_payload()
    payload[field] = unsafe_value
    reader = MarketDataHttpCatalogReader(
        "http://127.0.0.1:8000",
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                json={
                    "items": [payload],
                    "pagination": {"limit": 1, "offset": 0, "total": 1},
                },
            )
        ),
    )

    with pytest.raises(MarketDataHttpCatalogError, match="not safe for Control Plane") as captured:
        reader.list_datasets(limit=1, offset=0)
    assert secret not in str(captured.value)


class _FakeSignalResponse:
    def __init__(self, status: int, payload: dict[str, object]) -> None:
        self.status = status
        self._body = json.dumps(payload).encode()

    def read(self, _limit: int) -> bytes:
        return self._body


class _FakeSignalConnection:
    instances: list[_FakeSignalConnection] = []
    response_status = 200
    response_payload: dict[str, object] = {}

    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.request_args: tuple[str, str, dict[str, str]] | None = None
        self.closed = False
        self.__class__.instances.append(self)

    def request(self, method: str, path: str, *, headers: dict[str, str]) -> None:
        self.request_args = (method, path, headers)

    def getresponse(self) -> _FakeSignalResponse:
        return _FakeSignalResponse(self.response_status, self.response_payload)

    def close(self) -> None:
        self.closed = True


def _signal_payload(status: str = "ok") -> dict[str, object]:
    return {
        "status": status,
        "metrics": {
            "cycles": 12,
            "pairs_processed": 34,
            "signals_sent": 5,
            "pair_errors": 2,
            "last_cycle_at": "2026-08-07T04:19:00+00:00",
            "last_success_at": "2026-08-07T04:18:00+00:00",
            "errors_by_symbol": {
                "BTCUSDT": "TimeoutError",
                "ETHUSDT": "ValueError",
            },
        },
    }


def _install_signal_connection(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status: int,
    payload: dict[str, object],
) -> None:
    _FakeSignalConnection.instances.clear()
    _FakeSignalConnection.response_status = status
    _FakeSignalConnection.response_payload = payload
    monkeypatch.setattr(
        "modular_control_plane.adapters.signal_bot_http.HTTPConnection",
        _FakeSignalConnection,
    )


def test_signal_bot_health_uses_fixed_loopback_path_and_sanitizes_symbol_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signal_connection(monkeypatch, status=200, payload=_signal_payload())
    reader = SignalBotHttpHealthReader("http://127.0.0.1:8080", timeout_seconds=1.25)
    health = SignalBotHealthAdapter(reader, clock=fixed_clock).health()

    assert health.status is HealthStatus.HEALTHY
    assert health.details == {
        "cycles": 12,
        "pairs_processed": 34,
        "signals_sent": 5,
        "pair_errors": 2,
        "error_symbol_count": 2,
        "last_cycle_at": "2026-08-07T04:19:00+00:00",
        "last_success_at": "2026-08-07T04:18:00+00:00",
        "test_notification_available": False,
    }
    assert "BTCUSDT" not in health.model_dump_json()
    assert "ETHUSDT" not in health.model_dump_json()
    connection = _FakeSignalConnection.instances[0]
    assert connection.host == "127.0.0.1"
    assert connection.port == 8080
    assert connection.timeout == 1.25
    assert connection.request_args == ("GET", "/health", {"Accept": "application/json"})
    assert connection.closed is True


def test_signal_bot_degraded_503_is_a_valid_degraded_owner_snapshot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_signal_connection(
        monkeypatch,
        status=503,
        payload=_signal_payload(status="degraded"),
    )
    reader = SignalBotHttpHealthReader("http://localhost:8080")
    health = SignalBotHealthAdapter(reader, clock=fixed_clock).health()

    assert health.status is HealthStatus.DEGRADED
    assert health.summary == "Signal Bot loopback health is degraded"


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:8080",
        "http://10.0.0.1:8080",
        "http://example.com:8080",
        "http://user:token@127.0.0.1:8080",
        "http://127.0.0.1:8080/health",
        "http://127.0.0.1:8080?token=secret",
    ],
)
def test_signal_bot_url_rejects_nonlocal_or_secret_bearing_targets(url: str) -> None:
    with pytest.raises(ValueError):
        validate_signal_bot_base_url(url)


def test_signal_bot_health_contract_mismatch_fails_closed_without_source_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _signal_payload(status="ok")
    metrics = payload["metrics"]
    assert isinstance(metrics, dict)
    metrics["errors_by_symbol"] = {"private-symbol": "SecretError"}
    _install_signal_connection(monkeypatch, status=503, payload=payload)
    reader = SignalBotHttpHealthReader("http://127.0.0.1:8080")
    service = ControlPlaneService(
        [SignalBotHealthAdapter(reader, clock=fixed_clock)],
        clock=fixed_clock,
    )

    health = service.health(ComponentId.SIGNAL_BOT)
    rendered = health.model_dump_json()
    assert health.status is HealthStatus.UNAVAILABLE
    assert health.details["reason"] == "health_check_failed"
    assert "private-symbol" not in rendered
    assert "SecretError" not in rendered


def test_signal_bot_health_payload_has_no_telegram_configuration_surface(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = _signal_payload()
    payload["telegram_bot_token"] = "must-never-be-read"
    payload["telegram_admin_chat_id"] = 123456789
    _install_signal_connection(monkeypatch, status=200, payload=payload)
    health = SignalBotHealthAdapter(
        SignalBotHttpHealthReader("http://[::1]:8080"),
        clock=fixed_clock,
    ).health()
    rendered = health.model_dump_json()

    assert "must-never-be-read" not in rendered
    assert "123456789" not in rendered
    assert "telegram" not in rendered.lower()


def test_psutil_system_resources_provider_projects_only_bounded_aggregates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modular_control_plane.adapters.system_resources_psutil as provider_module

    class FakeProcess:
        def memory_info(self) -> SimpleNamespace:
            return SimpleNamespace(rss=123_456)

        def num_threads(self) -> int:
            return 7

    seen_intervals: list[float] = []
    seen_disks: list[str] = []

    def cpu_percent(*, interval: float) -> float:
        seen_intervals.append(interval)
        return 12.5

    def disk_usage(path: str) -> SimpleNamespace:
        seen_disks.append(path)
        return SimpleNamespace(total=100_000, used=25_000, free=75_000, percent=25.0)

    monkeypatch.setattr(provider_module.psutil, "cpu_percent", cpu_percent)
    monkeypatch.setattr(provider_module.psutil, "cpu_count", lambda *, logical: 8 if logical else 4)
    monkeypatch.setattr(
        provider_module.psutil,
        "virtual_memory",
        lambda: SimpleNamespace(total=16_000, used=6_000, available=10_000, percent=37.5),
    )
    monkeypatch.setattr(provider_module.psutil, "disk_usage", disk_usage)
    monkeypatch.setattr(provider_module.psutil, "boot_time", lambda: NOW.timestamp() - 3600.0)
    monkeypatch.setattr(provider_module.psutil, "Process", FakeProcess)
    monkeypatch.setattr(provider_module.psutil, "pids", lambda: [10, 20, 30])

    snapshot = PsutilSystemResourcesProvider(
        sample_interval_seconds=0.05,
        clock=fixed_clock,
    ).snapshot()
    rendered = snapshot.model_dump_json()

    assert snapshot == _system_snapshot().model_copy(update={"process_count": 3})
    assert seen_intervals == [0.05]
    assert seen_disks == ["/"]
    for forbidden in ("hostname", "username", "environment", "cmdline", "cwd", "/users/"):
        assert forbidden not in rendered.lower()


def test_system_resources_api_requires_reader_and_redacts_provider_failure() -> None:
    class StaticProvider:
        def snapshot(self) -> SystemResourcesSnapshot:
            return _system_snapshot()

    def authorize(request: Request) -> None:
        if request.headers.get("authorization") != "Bearer reader-token":
            raise HTTPException(status_code=401, detail="reader authentication required")

    app = create_api(ControlPlaneService(clock=fixed_clock), clock=fixed_clock)
    app.include_router(
        create_system_resources_router(
            SystemResourcesControlService(StaticProvider()),
            authorize_reader=authorize,
            clock=fixed_clock,
        )
    )
    client = TestClient(app)

    assert client.get("/api/control/v1/system/resources").status_code == 401
    response = client.get(
        "/api/control/v1/system/resources",
        headers={"Authorization": "Bearer reader-token"},
    )
    assert response.status_code == 200
    assert response.json()["cpu_percent"] == 12.5
    assert "hostname" not in response.text.lower()

    secret = "/Users/operator/private-system-secret"

    class BrokenProvider:
        def snapshot(self) -> SystemResourcesSnapshot:
            raise RuntimeError(secret)

    broken_app = create_api(ControlPlaneService(clock=fixed_clock), clock=fixed_clock)
    broken_app.include_router(
        create_system_resources_router(
            SystemResourcesControlService(BrokenProvider()),
            clock=fixed_clock,
        )
    )
    broken = TestClient(broken_app).get("/api/control/v1/system/resources")
    assert broken.status_code == 503
    assert broken.json()["error"]["technical_details"] == {"error_type": "RuntimeError"}
    assert secret not in broken.text


def test_control_client_reads_system_resources_from_fixed_loopback_path() -> None:
    token = "system-reader-secret"
    seen: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        seen.append((url, headers, timeout))
        return 200, _system_snapshot().model_dump_json().encode()

    client = ControlClient(
        ClientSettings(reader_token=token, timeout_seconds=1.5),
        transport=transport,
    )
    snapshot = client.system_resources()

    assert snapshot.cpu_percent == 12.5
    assert seen == [
        (
            "http://127.0.0.1:8765/api/control/v1/system/resources",
            {"Accept": "application/json", "Authorization": f"Bearer {token}"},
            1.5,
        )
    ]
    assert token not in repr(client)


def test_cli_wires_dataset_and_system_commands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class FakeTerminalClient:
        def __init__(self, _settings: ClientSettings) -> None:
            pass

        def datasets(self, *, limit: int = 50, offset: int = 0) -> MarketDatasetList:
            return MarketDatasetList(items=(), limit=limit, offset=offset, total=0)

        def system_resources(self) -> SystemResourcesSnapshot:
            return _system_snapshot()

    monkeypatch.setattr("modular_control_plane.cli.ControlClient", FakeTerminalClient)

    dataset_code = cli_main(["dataset", "list", "--json"])
    dataset_output = json.loads(capsys.readouterr().out)
    system_code = cli_main(["system", "resources", "--json"])
    system_output = json.loads(capsys.readouterr().out)

    assert dataset_code == 0
    assert dataset_output["total"] == 0
    assert system_code == 0
    assert system_output["cpu_percent"] == 12.5
