from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from modular_control_plane.server import ServerSettings, create_server
from modular_control_plane.service import ControlPlaneService

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
READER = "capacity-reader-token"
POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "docs/operations/STORAGE_PRESSURE_THRESHOLDS.json"
).resolve()


def _client() -> TestClient:
    return TestClient(
        create_server(
            ServerSettings(reader_token=READER),
            service=ControlPlaneService(clock=lambda: NOW),
        )
    )


def test_capacity_route_requires_reader_and_projects_canonical_policy(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", str(POLICY_PATH))
    client = _client()

    denied = client.get("/api/control/v1/system/capacity")
    accepted = client.get(
        "/api/control/v1/system/capacity",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "CONTROL_READER_AUTH_REQUIRED"
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    payload = accepted.json()
    assert payload["schema_version"] == 1
    assert payload["source_schema_version"] == 2
    assert payload["policy_status"] == "PARTIAL_MEASURED_FAIL_CLOSED"
    assert payload["global_local_disk_watermark_available"] is False
    assert len(payload["runner_watermarks"]) == 4
    assert len(payload["tiers"]) == 5
    rendered = json.dumps(payload)
    assert "sources" not in rendered
    assert "source_review" not in rendered
    assert "rationale" not in rendered
    assert str(POLICY_PATH) not in rendered


def test_missing_capacity_attachment_is_typed_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", raising=False)
    response = _client().get(
        "/api/control/v1/system/capacity",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    error = response.json()["error"]
    assert error["code"] == "CONTROL_SYSTEM_CAPACITY_NOT_ATTACHED"
    assert error["retryable"] is False
    assert error["technical_details"] == {}
    rendered = json.dumps(error)
    assert "MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH" not in rendered
    assert "/Users/" not in rendered


def test_malformed_capacity_policy_returns_safe_unavailable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_policy = tmp_path / "private-capacity-policy.json"
    private_policy.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "status": "PARTIAL_MEASURED_FAIL_CLOSED",
                "policy": {
                    "forecast_owner": "second_monitor",
                    "no_duplicate_monitor": True,
                    "unknown_means": "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM",
                },
                "tiers": {},
                "adoption_rule": {"unknown_is_not_deletion_permission": True},
                "secret": "do-not-project-this",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv(
        "MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH",
        str(private_policy.resolve()),
    )

    response = _client().get(
        "/api/control/v1/system/capacity",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert response.status_code == 503
    error = response.json()["error"]
    assert error["code"] == "CONTROL_SYSTEM_CAPACITY_UNAVAILABLE"
    assert error["retryable"] is True
    assert error["technical_details"] == {"error_type": "ValueError"}
    rendered = json.dumps(error)
    assert str(private_policy) not in rendered
    assert "do-not-project-this" not in rendered
    assert "second_monitor" not in rendered


def test_relative_capacity_policy_configuration_fails_server_composition(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", "relative/policy.json")

    try:
        _client()
    except ValueError as exc:
        assert "absolute" in str(exc)
        assert "relative/policy.json" not in str(exc)
    else:
        raise AssertionError("relative trusted policy configuration must fail closed")
