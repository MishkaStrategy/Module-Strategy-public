from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from modular_control_plane.cli import main
from modular_control_plane.client import ClientSettings
from modular_control_plane.server import ServerSettings, create_server
from modular_control_plane.service import ControlPlaneService
from modular_control_plane.system_artifact_capacity import (
    ExternalArtifactCapacitySnapshot,
    JsonExternalArtifactCapacityProvider,
)
from modular_control_plane.system_capacity_client import SystemCapacityControlClient

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
READER = "artifact-capacity-reader"
POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "docs/operations/STORAGE_PRESSURE_THRESHOLDS.json"
).resolve()

Policy = dict[str, Any]
PolicyMutator = Callable[[Policy], None]


def _policy() -> Policy:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise TypeError("canonical capacity policy must be an object")
    return cast(Policy, value)


def _write_policy(tmp_path: Path, value: Policy) -> Path:
    target = tmp_path / "capacity-policy.json"
    target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return target.resolve()


def _snapshot() -> ExternalArtifactCapacitySnapshot:
    return ExternalArtifactCapacitySnapshot(
        observed_at=NOW,
        source_schema_version=2,
        source_digest_sha256="b" * 64,
        kind="external_quota_and_maintenance_bound",
        status="PARTIAL_EXTERNAL_QUOTA",
        proactive_usage_status="UNKNOWN_NO_ACCEPTED_USAGE_SOURCE",
        critical_signal="CREATE_ARTIFACT_QUOTA_FAILURE",
        maintenance_max_count=400,
        maintenance_max_bytes=536_870_912,
        rate_limit_reserve_requests=100,
    )


def _set_artifact_quota(policy: Policy, key: str, value: object) -> None:
    policy["tiers"]["github_actions_artifacts"]["quota"][key] = value


def _set_artifact_maintenance(policy: Policy, key: str, value: object) -> None:
    policy["tiers"]["github_actions_artifacts"]["failed_cancelled_diagnostic_maintenance"][key] = (
        value
    )


def test_canonical_external_artifact_capacity_is_bounded_and_read_only() -> None:
    snapshot = JsonExternalArtifactCapacityProvider(
        POLICY_PATH,
        clock=lambda: NOW,
    ).snapshot()

    assert snapshot.status == "PARTIAL_EXTERNAL_QUOTA"
    assert snapshot.proactive_usage_status == "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE"
    assert snapshot.critical_signal == "CREATE_ARTIFACT_QUOTA_FAILURE"
    assert snapshot.maintenance_max_count == 400
    assert snapshot.maintenance_max_bytes == 536_870_912
    assert snapshot.rate_limit_reserve_requests == 100
    assert snapshot.local_disk_watermark_applicable is False
    assert snapshot.operator_cleanup_available is False
    rendered = snapshot.model_dump_json()
    assert "sources" not in rendered
    assert "ordering" not in rendered
    assert "delete_delay" not in rendered
    assert "workflow" not in rendered
    assert "/Users/" not in rendered


@pytest.mark.parametrize(
    ("mutator", "message"),
    [
        (
            lambda policy: _set_artifact_quota(policy, "high_water", 123),
            "high-water must remain unknown",
        ),
        (
            lambda policy: _set_artifact_quota(policy, "proactive_usage_status", "KNOWN"),
            "proactive usage must remain unknown",
        ),
        (
            lambda policy: _set_artifact_maintenance(policy, "bound_type", "QUOTA_WATERMARK"),
            "cannot be treated as quota watermark",
        ),
        (
            lambda policy: _set_artifact_maintenance(policy, "requires_exact_batch_sha256", False),
            "exact batch digest requirement",
        ),
    ],
)
def test_external_artifact_policy_tampering_fails_closed(
    tmp_path: Path,
    mutator: PolicyMutator,
    message: str,
) -> None:
    policy = _policy()
    mutator(policy)
    with pytest.raises(ValueError, match=message):
        JsonExternalArtifactCapacityProvider(
            _write_policy(tmp_path, policy),
            clock=lambda: NOW,
        ).snapshot()


def test_external_artifact_capacity_api_requires_reader_and_exposes_no_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", str(POLICY_PATH))
    client = TestClient(
        create_server(
            ServerSettings(reader_token=READER),
            service=ControlPlaneService(clock=lambda: NOW),
        )
    )

    denied = client.get("/api/control/v1/system/capacity/artifacts")
    accepted = client.get(
        "/api/control/v1/system/capacity/artifacts",
        headers={"Authorization": f"Bearer {READER}"},
    )

    assert denied.status_code == 401
    assert accepted.status_code == 200
    assert accepted.headers["cache-control"] == "no-store"
    payload = accepted.json()
    assert payload["operator_cleanup_available"] is False
    assert payload["local_disk_watermark_applicable"] is False
    assert payload["proactive_usage_status"] == "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE"
    rendered = json.dumps(payload)
    assert "MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH" not in rendered
    assert "actions_artifact_prune_batch" not in rendered


def test_external_artifact_capacity_client_uses_fixed_route() -> None:
    snapshot = _snapshot()
    seen: list[str] = []

    def transport(url: str, _headers: dict[str, str], _timeout: float) -> tuple[int, bytes]:
        seen.append(url)
        return 200, snapshot.model_dump_json().encode()

    client = SystemCapacityControlClient(
        ClientSettings(reader_token=READER),
        transport=transport,
    )

    assert client.external_artifact_capacity() == snapshot
    assert seen == ["http://127.0.0.1:8765/api/control/v1/system/capacity/artifacts"]


def test_external_artifact_capacity_cli_is_explicitly_non_mutating(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = _snapshot()

    class FakeCapacityClient:
        def __init__(self, _settings: ClientSettings) -> None:
            pass

        def external_artifact_capacity(self) -> ExternalArtifactCapacitySnapshot:
            return snapshot

    monkeypatch.setattr(
        "modular_control_plane.cli_system.SystemCapacityControlClient",
        FakeCapacityClient,
    )

    assert main(["system", "artifact-capacity"]) == 0
    output = capsys.readouterr().out
    assert "PARTIAL_EXTERNAL_QUOTA" in output
    assert "UNKNOWN_NO_ACCEPTED_USAGE_SOURCE" in output
    assert "CREATE_ARTIFACT_QUOTA_FAILURE" in output
    assert "physical disk watermark not applicable" in output
    assert "CLEANUP    unavailable in Control UI" in output
    assert "delete" not in output.lower()
