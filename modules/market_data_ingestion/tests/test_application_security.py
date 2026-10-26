from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.contracts import StorageFormat
from market_data_application.security import ApiRole, ApiTokenAuthenticator
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType

_READER = "reader-token-00000000000000000000000000000000"
_OPERATOR = "operator-token-000000000000000000000000000000"
_ADMIN = "admin-token-00000000000000000000000000000000"


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
                instance_id="security-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_api_requires_valid_token_when_authenticator_is_configured(tmp_path):
    app = application(tmp_path)
    authenticator = ApiTokenAuthenticator(
        {
            _READER: ApiRole.READER,
            _OPERATOR: ApiRole.OPERATOR,
            _ADMIN: ApiRole.ADMIN,
        }
    )
    try:
        with pytest.raises(ValueError, match="exactly one"):
            create_api(app)
        with pytest.raises(ValueError, match="exactly one"):
            create_api(app, authenticator=authenticator, control_plane=object())
        client = TestClient(create_api(app, authenticator=authenticator))
        public = client.get("/api/v1/version")
        missing = client.get("/api/v1/jobs")
        invalid = client.get("/api/v1/jobs", headers=bearer("x" * 32))
        allowed = client.get("/api/v1/jobs", headers=bearer(_READER))

        assert public.status_code == 200
        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert missing.json()["error"]["code"] == "API_AUTHENTICATION_REQUIRED"
        assert invalid.status_code == 401
        assert allowed.status_code == 200
    finally:
        app.shutdown()


def test_reader_cannot_submit_but_operator_can(tmp_path):
    app = application(tmp_path)
    authenticator = ApiTokenAuthenticator(
        {
            _READER: ApiRole.READER,
            _OPERATOR: ApiRole.OPERATOR,
            _ADMIN: ApiRole.ADMIN,
        }
    )
    try:
        client = TestClient(create_api(app, authenticator=authenticator))
        reader = client.post(
            "/api/v1/downloads",
            headers={**bearer(_READER), "Idempotency-Key": "reader-denied"},
            json=request_model().model_dump(mode="json"),
        )
        operator = client.post(
            "/api/v1/downloads",
            headers={**bearer(_OPERATOR), "Idempotency-Key": "operator-allowed"},
            json=request_model().model_dump(mode="json"),
        )

        assert reader.status_code == 403
        assert reader.json()["error"]["code"] == "API_ACCESS_DENIED"
        management_unavailable = client.get("/api/v1/security/tokens", headers=bearer(_ADMIN))
        assert management_unavailable.status_code == 503
        assert operator.status_code == 202
    finally:
        app.shutdown()


def test_authenticator_rejects_short_tokens():
    try:
        ApiTokenAuthenticator({"short": ApiRole.ADMIN})
    except ValueError as error:
        assert "between 32 and 512" in str(error)
    else:
        raise AssertionError("short token was accepted")
