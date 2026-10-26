from datetime import UTC, datetime

import pytest
from auth_helpers import LOCAL_ADMIN_HEADERS, local_admin_authenticator
from fastapi.testclient import TestClient

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.contracts import StorageFormat
from market_data_application.security import ApiRole
from market_data_application.server import (
    authentication_mode_from_environment,
    authenticator_from_environment,
    create_server,
    main,
    settings_from_environment,
    validate_bind_host,
)
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType, RunState


def request_model() -> CreateDownloadRequest:
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
        storage_format=StorageFormat.CSV,
        compression="zstd",
        concurrency=1,
    )


def application(tmp_path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="server-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def test_server_lifespan_recovers_created_jobs(tmp_path):
    app = application(tmp_path)
    created = app.create_download(
        request_model(),
        idempotency_key="server-recovery",
    )
    try:
        with TestClient(create_server(app, authenticator=local_admin_authenticator())) as client:
            health = client.get(
                "/api/v1/health",
                headers=LOCAL_ADMIN_HEADERS,
            )
            completed = app.wait(created.job.job_id, timeout=30)
            assert health.status_code == 200
            assert completed.state in {RunState.COMPLETED, RunState.COMPLETED_WITH_WARNINGS}
    finally:
        app.shutdown()


def test_server_environment_settings_are_validated(tmp_path):
    settings = settings_from_environment(
        {
            "MDI_STORAGE_ROOT": str(tmp_path),
            "MDI_WORKER_COUNT": "3",
            "MDI_INSTANCE_ID": "api-test",
            "MDI_INCLUDE_FAKE_ADAPTER": "true",
        }
    )
    assert settings.storage_root == tmp_path.resolve()
    assert settings.worker_count == 3
    assert settings.instance_id == "api-test"
    assert settings.include_fake_adapter is True

    with pytest.raises(ValueError, match="boolean environment"):
        settings_from_environment({"MDI_INCLUDE_FAKE_ADAPTER": "maybe"})


def test_server_bind_host_is_loopback_only():
    assert validate_bind_host("127.0.0.1") == "127.0.0.1"
    assert validate_bind_host("localhost") == "localhost"
    assert validate_bind_host("::1") == "::1"
    with pytest.raises(ValueError, match="loopback"):
        validate_bind_host("0.0.0.0")


def test_server_check_mode_does_not_start_uvicorn(capsys, monkeypatch):
    token = "synthetic-check-admin-token-000000000000000000"
    monkeypatch.setenv(
        "MDI_API_TOKENS_JSON",
        f'{{"{token}":"admin"}}',
    )
    assert main(["--check", "--host", "127.0.0.1", "--port", "8080"]) == 0
    payload = capsys.readouterr().out
    assert '"remote_bind_allowed": false' in payload
    assert '"port": 8080' in payload
    assert token not in payload


def test_authentication_mode_requires_one_valid_provider_without_state_writes(tmp_path):
    bearer = {"MDI_API_TOKENS_JSON": '{"synthetic-token-value-000000000000000000":"admin"}'}
    assert authentication_mode_from_environment(bearer) == "legacy_bearer"
    assert (
        authentication_mode_from_environment(
            {
                "MDI_STORAGE_ROOT": str(tmp_path),
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": (b"x" * 32).hex(),
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": '{"generation":1,"tokens":[{"token_id":"admin","token":"synthetic-token-value-000000000000000000","role":"admin"}]}',
            }
        )
        == "hardened_control_plane"
    )
    assert list(tmp_path.iterdir()) == []
    with pytest.raises(ValueError, match="requires bearer"):
        authentication_mode_from_environment({})
    with pytest.raises(ValueError, match="at least one API token"):
        authentication_mode_from_environment({"MDI_API_TOKENS_JSON": "{}"})
    with pytest.raises(ValueError, match="must not be configured together"):
        authentication_mode_from_environment(
            {
                **bearer,
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": (b"x" * 32).hex(),
            }
        )
    with pytest.raises(ValueError, match="AUDIT_KEY_HEX is required"):
        authentication_mode_from_environment(
            {"MDI_CONTROL_PLANE_BOOTSTRAP_JSON": '{"generation":1,"tokens":[]}'}
        )
    with pytest.raises(ValueError, match="valid JSON"):
        authentication_mode_from_environment(
            {
                "MDI_CONTROL_PLANE_AUDIT_KEY_HEX": (b"x" * 32).hex(),
                "MDI_CONTROL_PLANE_BOOTSTRAP_JSON": "{",
            }
        )


def test_api_server_missing_auth_fails_before_settings_or_application_construction(monkeypatch):
    from market_data_application import server

    for name in (
        "MDI_API_TOKENS_JSON",
        "MDI_CONTROL_PLANE_AUDIT_KEY_HEX",
        "MDI_CONTROL_PLANE_BOOTSTRAP_JSON",
    ):
        monkeypatch.delenv(name, raising=False)

    def unexpected_context(*_args, **_kwargs):
        raise AssertionError("application context must not be created")

    def unexpected_settings(*_args, **_kwargs):
        raise AssertionError("application settings must not be constructed")

    monkeypatch.setattr(server, "settings_from_environment", unexpected_settings)
    monkeypatch.setattr(server, "build_application_context", unexpected_context)
    with pytest.raises(ValueError, match="requires bearer"):
        main([])
    with pytest.raises(ValueError, match="requires bearer"):
        main(["--check"])


def test_server_factory_requires_exactly_one_provider_before_owned_app_creation(monkeypatch):
    from market_data_application import server

    def unexpected_context(*_args, **_kwargs):
        raise AssertionError("application context must not be created")

    monkeypatch.setattr(server, "build_application_context", unexpected_context)
    with pytest.raises(ValueError, match="exactly one"):
        server.create_server()
    with pytest.raises(ValueError, match="exactly one"):
        server.create_server(
            authenticator=local_admin_authenticator(),
            control_plane=object(),  # type: ignore[arg-type]
        )


def test_server_parses_token_roles_without_exposing_tokens():
    token = "admin-token-00000000000000000000000000000000"
    authenticator = authenticator_from_environment(
        {"MDI_API_TOKENS_JSON": f'{{"{token}":"admin"}}'}
    )
    assert authenticator is not None
    principal = authenticator.authenticate(token, required_role=ApiRole.ADMIN)
    assert principal.role is ApiRole.ADMIN
    assert token not in repr(authenticator)
