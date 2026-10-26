from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from market_data_application import (
    ApplicationSettings,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.control_plane_http import ControlPlaneSecurityController
from market_data_application.control_plane_security import (
    ApiTokenRecord,
    ApiTokenSnapshot,
    ReloadableApiTokenAuthenticator,
    TokenSnapshotState,
)
from market_data_application.control_plane_store import AuditContext, SQLiteControlPlaneStore
from market_data_application.security import ApiRole
from market_data_application.server import create_server, main
from market_data_ingestion.adapters import FakeExchangeAdapter

NOW = datetime(2026, 7, 30, 2, 0, tzinfo=UTC)
ADMIN_TOKEN = "server-admin-token-0000000000000000000000000"
AUDIT_KEY = b"server-control-plane-audit-key-at-least-32-bytes"


def _application(tmp_path: Path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=tmp_path,
                instance_id="hardened-server-tests",
                worker_count=1,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def _controller(tmp_path: Path) -> ControlPlaneSecurityController:
    record = ApiTokenRecord.from_plaintext(
        token_id="server-admin",
        token=ADMIN_TOKEN,
        role=ApiRole.ADMIN,
        not_before=NOW,
    )
    state = TokenSnapshotState(
        current=ApiTokenSnapshot(generation=1, created_at=NOW, records=(record,))
    )
    store = SQLiteControlPlaneStore(tmp_path / "control-plane.sqlite3", audit_key=AUDIT_KEY)
    store.bootstrap(
        state,
        audit=AuditContext(
            timestamp=NOW,
            correlation_id="corr-server-bootstrap",
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
        clock=lambda: NOW,
    )


def test_create_server_wires_hardened_controller(tmp_path: Path) -> None:
    application = _application(tmp_path)
    controller = _controller(tmp_path)
    try:
        with TestClient(create_server(application, control_plane=controller)) as client:
            missing = client.get("/api/v1/jobs")
            allowed = client.get(
                "/api/v1/jobs",
                headers={"Authorization": f"Bearer {ADMIN_TOKEN}"},
            )
        assert missing.status_code == 401
        assert allowed.status_code == 200
    finally:
        application.shutdown()


def test_check_mode_reports_hardened_configuration_without_bootstrapping(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    bootstrap = json.dumps(
        {
            "generation": 1,
            "tokens": [
                {
                    "token_id": "server-admin",
                    "token": ADMIN_TOKEN,
                    "role": "admin",
                }
            ],
        }
    )
    monkeypatch.setenv("MDI_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("MDI_CONTROL_PLANE_AUDIT_KEY_HEX", AUDIT_KEY.hex())
    monkeypatch.setenv("MDI_CONTROL_PLANE_BOOTSTRAP_JSON", bootstrap)

    assert main(["--check", "--host", "127.0.0.1", "--port", "8081"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["authentication_enabled"] is True
    assert payload["authentication_mode"] == "hardened_control_plane"
    assert payload["remote_bind_allowed"] is False
    assert ADMIN_TOKEN not in repr(payload)
    assert not (tmp_path / "control-plane.sqlite3").exists()


def test_check_mode_rejects_legacy_and_hardened_auth_together(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MDI_STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("MDI_CONTROL_PLANE_AUDIT_KEY_HEX", AUDIT_KEY.hex())
    monkeypatch.setenv("MDI_API_TOKENS_JSON", json.dumps({ADMIN_TOKEN: "admin"}))
    with pytest.raises(ValueError, match="must not be configured together"):
        main(["--check"])
