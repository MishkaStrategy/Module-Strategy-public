from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from market_data_application import (
    ApplicationSettings,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.api import create_api
from market_data_application.control_plane_http import ControlPlaneSecurityController
from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    BoundedTokenBucketLimiter,
    ControlPlaneLimits,
    ReloadableApiTokenAuthenticator,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import AuditContext, SQLiteControlPlaneStore
from market_data_application.security import ApiRole
from market_data_ingestion.adapters import FakeExchangeAdapter

NOW = datetime(2026, 7, 30, 1, 0, tzinfo=UTC)
ADMIN_OLD = "api-admin-old-token-000000000000000000000000"
ADMIN_NEW = "api-admin-new-token-000000000000000000000000"
READER = "api-reader-token-000000000000000000000000000"
AUDIT_KEY = b"api-integration-audit-key-at-least-thirty-two-bytes"


def _record(token_id: str, token: str, role: ApiRole) -> ApiTokenRecord:
    return ApiTokenRecord.from_plaintext(
        token_id=token_id,
        token=token,
        role=role,
        not_before=NOW,
    )


def _application(tmp_path: Path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="control-plane-api-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def _controller(
    tmp_path: Path,
    *,
    peer_capacity: int = 100,
    body_bytes: int = 2 * 1024 * 1024,
    max_sse_per_principal: int = 1,
) -> ControlPlaneSecurityController:
    state = TokenSnapshotState(
        current=ApiTokenSnapshot(
            generation=1,
            created_at=NOW,
            records=(
                _record("admin-old", ADMIN_OLD, ApiRole.ADMIN),
                _record("reader", READER, ApiRole.READER),
            ),
        )
    )
    store = SQLiteControlPlaneStore(tmp_path / "security.sqlite3", audit_key=AUDIT_KEY)
    store.bootstrap(
        state,
        audit=AuditContext(
            timestamp=NOW,
            correlation_id="corr-bootstrap",
            direct_peer="local_process",
            principal_id="bootstrap",
            token_id="bootstrap",
            action="security.bootstrap",
            outcome="accepted",
        ),
    )
    return ControlPlaneSecurityController(
        authenticator=ReloadableApiTokenAuthenticator(state, clock=lambda: NOW),
        store=store,
        limits=ControlPlaneLimits(
            max_body_bytes=body_bytes,
            max_sse_global=4,
            max_sse_per_principal=max_sse_per_principal,
            max_sse_per_peer=4,
        ),
        peer_limiter=BoundedTokenBucketLimiter(
            capacity=peer_capacity,
            refill_per_second=1.0,
            max_entries=64,
            clock=lambda: 0.0,
        ),
        principal_limiter=BoundedTokenBucketLimiter(
            capacity=100,
            refill_per_second=1.0,
            max_entries=64,
            clock=lambda: 0.0,
        ),
        clock=lambda: NOW,
    )


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "X-Correlation-ID": "corr-client"}


def test_hardened_api_audits_auth_and_exposes_only_admin_security_state(tmp_path: Path) -> None:
    application = _application(tmp_path)
    controller = _controller(tmp_path)
    try:
        client = TestClient(create_api(application, control_plane=controller))
        missing = client.get("/api/v1/jobs")
        reader = client.get("/api/v1/jobs", headers=_bearer(READER))
        denied = client.get("/api/v1/security/tokens", headers=_bearer(READER))
        state = client.get("/api/v1/security/tokens", headers=_bearer(ADMIN_OLD))

        assert missing.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"
        assert missing.headers["x-correlation-id"].startswith("corr_api_")
        assert reader.status_code == 200
        assert reader.headers["x-correlation-id"] == "corr-client"
        assert denied.status_code == 403
        assert state.status_code == 200
        assert state.json()["generation"] == 1
        assert {record["token_id"] for record in state.json()["records"]} == {
            "admin-old",
            "reader",
        }
        assert ADMIN_OLD not in state.text
        assert READER not in state.text

        actions = [record.action for record in controller.store.audit_records()]
        assert "security.authentication" in actions
        assert "security.authorization" in actions
    finally:
        application.shutdown()


def test_hardened_api_rotates_revokes_and_returns_verified_audit(tmp_path: Path) -> None:
    application = _application(tmp_path)
    controller = _controller(tmp_path)
    try:
        client = TestClient(create_api(application, control_plane=controller))
        reload_response = client.post(
            "/api/v1/security/tokens/reload",
            headers=_bearer(ADMIN_OLD),
            json={
                "generation": 2,
                "overlap_seconds": 30,
                "tokens": [
                    {
                        "token_id": "admin-new",
                        "token": ADMIN_NEW,
                        "role": "admin",
                    }
                ],
            },
        )
        assert reload_response.status_code == 200
        assert reload_response.json()["generation"] == 2
        assert ADMIN_NEW not in reload_response.text

        revoke_response = client.post(
            "/api/v1/security/tokens/admin-old/revoke",
            headers=_bearer(ADMIN_NEW),
            json={"generation": 3},
        )
        assert revoke_response.status_code == 200
        assert revoke_response.json()["generation"] == 3

        revoked = client.get("/api/v1/jobs", headers=_bearer(ADMIN_OLD))
        current = client.get("/api/v1/jobs", headers=_bearer(ADMIN_NEW))
        audit = client.get(
            "/api/v1/security/audit?limit=100",
            headers=_bearer(ADMIN_NEW),
        )
        assert revoked.status_code == 401
        assert current.status_code == 200
        assert audit.status_code == 200
        assert [record["action"] for record in audit.json()][-3:] == [
            "security.token_reload",
            "security.token_revoke",
            "security.authentication",
        ]
        assert ADMIN_OLD not in audit.text
        assert ADMIN_NEW not in audit.text
    finally:
        application.shutdown()


def test_hardened_api_returns_stable_rate_and_body_admission_errors(tmp_path: Path) -> None:
    rate_application = _application(tmp_path / "rate")
    rate_controller = _controller(tmp_path / "rate", peer_capacity=1)
    try:
        rate_client = TestClient(create_api(rate_application, control_plane=rate_controller))
        assert rate_client.get("/api/v1/jobs", headers=_bearer(ADMIN_OLD)).status_code == 200
        limited = rate_client.get("/api/v1/jobs", headers=_bearer(ADMIN_OLD))
        assert limited.status_code == 429
        assert limited.headers["retry-after"] == "1"
        assert limited.json()["error"]["code"] == "API_RATE_LIMITED"
    finally:
        rate_application.shutdown()

    body_application = _application(tmp_path / "body")
    body_controller = _controller(tmp_path / "body", body_bytes=64)
    try:
        body_client = TestClient(create_api(body_application, control_plane=body_controller))
        oversized = body_client.post(
            "/api/v1/security/tokens/reload",
            headers=_bearer(ADMIN_OLD),
            content=b"x" * 65,
        )
        assert oversized.status_code == 413
        assert oversized.json()["error"]["code"] == "REQUEST_BODY_TOO_LARGE"
    finally:
        body_application.shutdown()


def test_hardened_sse_lease_is_released_after_finite_stream(tmp_path: Path) -> None:
    application = _application(tmp_path)
    controller = _controller(tmp_path, max_sse_per_principal=1)
    try:
        client = TestClient(create_api(application, control_plane=controller))
        created = application.create_download(
            {
                "exchange_id": "fake",
                "market_type": "usdt_m_perpetual",
                "symbol": "BTCUSDT",
                "required_data": ["klines"],
                "optional_data": [],
                "base_timeframe": "5m",
                "derived_timeframes": [],
                "start": "2026-01-01T00:00:00Z",
                "end": "2026-01-01T00:10:00Z",
                "storage_format": "csv",
                "compression": "zstd",
                "concurrency": 1,
            },
            idempotency_key="sse-security",
        )
        path = f"/api/v1/jobs/{created.job.job_id}/events/stream"
        first = client.get(path, headers=_bearer(READER))
        second = client.get(path, headers=_bearer(READER))
        assert first.status_code == 200
        assert second.status_code == 200
    finally:
        application.shutdown()
