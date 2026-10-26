from __future__ import annotations

import builtins
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modular_control_plane import (
    BacktestControlService,
    BacktestExecution,
    BacktestRunRequest,
    ComponentId,
    ControlPlaneService,
    JobStatus,
    SQLiteBacktestJobStore,
)
from modular_control_plane.adapters.backtest import ManagedBacktestRunner
from modular_control_plane.backtest import BacktestRunnerError
from modular_control_plane.server import (
    ServerSettings,
    build_backtest_control,
    build_service,
    create_server,
    main,
    settings_from_environment,
    validate_bind_host,
    validate_port,
)

NOW = datetime(2026, 8, 7, 4, 30, tzinfo=UTC)


def fixed_clock() -> datetime:
    return NOW


class RecoveryRunner:
    def __init__(self, root: Path) -> None:
        self._root = root

    def run(self, config_id: str) -> BacktestExecution:
        result = self._root / f"run-{config_id}"
        result.mkdir(parents=True, exist_ok=True)
        return BacktestExecution(
            run_id=result.name,
            output_directory=result,
            bars_processed=42,
        )


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.50", "control.example.com", "*"])
def test_server_rejects_non_loopback_bind(host: str) -> None:
    with pytest.raises(ValueError, match="loopback"):
        validate_bind_host(host)


def test_server_accepts_only_supported_loopback_bind_forms() -> None:
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("LOCALHOST") == "localhost"
    assert validate_bind_host("::1") == "::1"


@pytest.mark.parametrize("port", [0, -1, 65536, 100000])
def test_server_rejects_invalid_port(port: int) -> None:
    with pytest.raises(ValueError, match="port"):
        validate_port(port)


def test_server_settings_never_render_reader_or_operator_tokens() -> None:
    settings = ServerSettings(
        market_data_reader_token="market-data-secret",
        reader_token="control-reader-secret",
        operator_token="control-operator-secret",
    )
    rendered = repr(settings)

    assert "market-data-secret" not in rendered
    assert "control-reader-secret" not in rendered
    assert "control-operator-secret" not in rendered
    assert "market_data_reader_token" not in rendered
    assert "reader_token" not in rendered
    assert "operator_token" not in rendered


def test_environment_configuration_is_paper_local_and_validated() -> None:
    settings = settings_from_environment(
        {
            "MODULAR_CONTROL_HOST": "localhost",
            "MODULAR_CONTROL_PORT": "8766",
            "MODULAR_CONTROL_MARKET_DATA_URL": "http://127.0.0.1:8100",
            "MODULAR_CONTROL_MARKET_DATA_READER_TOKEN": "mdi-token",
            "MODULAR_CONTROL_SIGNAL_BOT_URL": "http://127.0.0.1:8180",
            "MODULAR_CONTROL_READER_TOKEN": "reader-token",
            "MODULAR_CONTROL_BACKTEST_WORKERS": "2",
            "MODULAR_CONTROL_REQUEST_TIMEOUT_SECONDS": "1.5",
            "MODULAR_CONTROL_LOG_LEVEL": "warning",
        }
    )

    assert settings.host == "localhost"
    assert settings.port == 8766
    assert settings.market_data_url == "http://127.0.0.1:8100"
    assert settings.signal_bot_url == "http://127.0.0.1:8180"
    assert settings.backtest_worker_count == 2
    assert settings.request_timeout_seconds == 1.5
    assert settings.log_level == "warning"
    assert settings.backtest_config_root is None


@pytest.mark.parametrize("workers", [0, 5, 100])
def test_backtest_worker_count_is_bounded(workers: int) -> None:
    with pytest.raises(ValueError, match="backtest_worker_count"):
        ServerSettings(backtest_worker_count=workers)


def test_backtest_control_requires_explicit_operator_authentication(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    config_root.mkdir()

    with pytest.raises(ValueError, match="operator_token"):
        ServerSettings(
            backtest_config_root=config_root,
            backtest_output_root=tmp_path / "results",
            backtest_state_path=tmp_path / "state.sqlite3",
        )


def test_backtest_control_registers_component_without_importing_engine(tmp_path: Path) -> None:
    config_root = tmp_path / "configs"
    config_root.mkdir()
    settings = ServerSettings(
        operator_token="operator-token",
        backtest_config_root=config_root,
        backtest_output_root=tmp_path / "results",
        backtest_state_path=tmp_path / "state.sqlite3",
    )

    backtest = build_backtest_control(settings)
    assert backtest is not None
    service = build_service(settings, backtest_control=backtest)
    assert service.registered_components == (ComponentId.BACKTEST, ComponentId.MARKET_DATA)


@pytest.mark.parametrize("operation", ["run", "inspect", "compare"])
def test_backtest_owner_dependency_failure_is_lazy_actionable_and_redacted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    config_root = tmp_path / "configs"
    output_root = tmp_path / "results"
    config_root.mkdir()
    output_root.mkdir()
    (config_root / "case.yaml").write_text("{}\n", encoding="utf-8")
    left = output_root / "left"
    right = output_root / "right"
    left.mkdir()
    right.mkdir()
    runner = ManagedBacktestRunner(config_root, output_root)

    original_import = builtins.__import__

    def reject_backtest_import(
        name: str,
        global_values: dict[str, object] | None = None,
        local_values: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "modular_backtest" or name.startswith("modular_backtest."):
            raise ImportError("/private/owner/path contains secret-marker")
        return original_import(name, global_values, local_values, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", reject_backtest_import)

    with pytest.raises(BacktestRunnerError) as captured:
        if operation == "run":
            runner.run("case")
        elif operation == "inspect":
            runner.inspect(left)
        else:
            runner.compare(left, right)

    error = captured.value
    assert error.code == "BACKTEST_DEPENDENCY_UNAVAILABLE"
    assert error.retryable is False
    assert (
        error.action
        == "Install the Backtest owner package before enabling or retrying Backtest Control."
    )
    assert error.details == {"error_type": "DependencyUnavailable"}
    rendered = f"{error} {error.safe_message} {error.action} {error.details}"
    assert "/private/owner/path" not in rendered
    assert "secret-marker" not in rendered


def test_default_service_registers_only_real_market_data_adapter_but_requires_full_surface() -> (
    None
):
    service = build_service(ServerSettings())

    assert service.registered_components == (ComponentId.MARKET_DATA,)


def test_signal_bot_health_adapter_is_registered_only_when_explicitly_configured() -> None:
    service = build_service(ServerSettings(signal_bot_url="http://127.0.0.1:8180"))

    assert service.registered_components == (ComponentId.MARKET_DATA, ComponentId.SIGNAL_BOT)


def test_reader_token_guards_control_plane_reads_with_typed_actionable_error() -> None:
    secret = "reader-token-value"
    settings = ServerSettings(reader_token=secret)
    service = ControlPlaneService(clock=fixed_clock)
    client = TestClient(create_server(settings, service=service))

    denied = client.get("/api/control/v1/version")
    accepted = client.get(
        "/api/control/v1/version",
        headers={"Authorization": f"Bearer {secret}"},
    )

    assert denied.status_code == 401
    assert denied.headers["www-authenticate"] == "Bearer"
    assert denied.headers["cache-control"] == "no-store"
    error = denied.json()["error"]
    assert error["code"] == "CONTROL_READER_AUTH_REQUIRED"
    assert error["operation"] == "reader_authentication"
    assert error["retryable"] is True
    assert error["action"] == "Provide the configured Control reader token and retry the read."
    assert error["technical_details"] == {}
    assert secret not in denied.text
    assert accepted.status_code == 200
    assert secret not in accepted.text


def test_server_startup_recovers_persisted_queued_backtest(tmp_path: Path) -> None:
    store = SQLiteBacktestJobStore(tmp_path / "backtests.sqlite3", clock=fixed_clock)
    control = BacktestControlService(
        store,
        RecoveryRunner(tmp_path / "results"),  # type: ignore[arg-type]
        clock=fixed_clock,
    )
    created = control.create_run(
        BacktestRunRequest(config_id="recovered"),
        idempotency_key="recover-on-startup",
        actor_id="local-operator",
    ).record
    assert created.job.status is JobStatus.QUEUED

    settings = ServerSettings(operator_token="operator-token", backtest_worker_count=1)
    service = ControlPlaneService([control], clock=fixed_clock)
    application = create_server(settings, service=service, backtest_control=control)

    with TestClient(application) as client:
        deadline = time.monotonic() + 2.0
        payload: dict[str, object] = {}
        while time.monotonic() < deadline:
            response = client.get(f"/api/control/v1/backtests/{created.job.job_id}")
            assert response.status_code == 200
            payload = response.json()
            job = payload.get("job")
            if isinstance(job, dict) and job.get("status") == "SUCCEEDED":
                break
            time.sleep(0.01)

    assert isinstance(payload.get("job"), dict)
    assert payload["job"]["status"] == "SUCCEEDED"  # type: ignore[index]
    assert isinstance(payload.get("result"), dict)
    assert payload["result"]["run_id"] == "run-recovered"  # type: ignore[index]


def test_check_output_contains_configuration_state_not_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    config_root = tmp_path / "backtest-configs"
    config_root.mkdir()
    monkeypatch.setenv("MODULAR_CONTROL_MARKET_DATA_READER_TOKEN", "mdi-secret")
    monkeypatch.setenv("MODULAR_CONTROL_READER_TOKEN", "reader-secret")
    monkeypatch.setenv("MODULAR_CONTROL_OPERATOR_TOKEN", "operator-secret")
    monkeypatch.setenv("MODULAR_CONTROL_SIGNAL_BOT_URL", "http://127.0.0.1:8180")
    monkeypatch.setenv("MODULAR_CONTROL_BACKTEST_CONFIG_ROOT", str(config_root))
    monkeypatch.setenv("MODULAR_CONTROL_BACKTEST_OUTPUT_ROOT", str(tmp_path / "results"))
    monkeypatch.setenv("MODULAR_CONTROL_BACKTEST_STATE_PATH", str(tmp_path / "state.sqlite3"))
    monkeypatch.setenv("MODULAR_CONTROL_BACKTEST_WORKERS", "2")

    assert main(["--check"]) == 0
    output = capsys.readouterr().out

    assert '"mode": "PAPER"' in output
    assert '"remote_bind_allowed": false' in output
    assert '"market_data_token_configured": true' in output
    assert '"signal_bot_health_enabled": true' in output
    assert '"reader_authentication_enabled": true' in output
    assert '"operator_authentication_enabled": true' in output
    assert '"backtest_control_enabled": true' in output
    assert '"backtest_worker_count": 2' in output
    assert '"system_resources_enabled": true' in output
    assert "mdi-secret" not in output
    assert "reader-secret" not in output
    assert "operator-secret" not in output
