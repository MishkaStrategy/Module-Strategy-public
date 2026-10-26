from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from modular_control_plane.cli import main
from modular_control_plane.client import ClientSettings, ControlClientError
from modular_control_plane.system_capacity import (
    CapacityPhysicalBoundary,
    CapacityTierId,
    RunnerCapacityRole,
    RunnerCapacityWatermark,
    SystemCapacitySnapshot,
    SystemCapacityTier,
)
from modular_control_plane.system_capacity_client import SystemCapacityControlClient

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)


def _snapshot() -> SystemCapacitySnapshot:
    return SystemCapacitySnapshot(
        observed_at=NOW,
        source_schema_version=2,
        source_digest_sha256="a" * 64,
        policy_status="PARTIAL_MEASURED_FAIL_CLOSED",
        unknown_means="NO_DESTRUCTIVE_OR_CAPACITY_CLAIM",
        runner_watermarks=(
            RunnerCapacityWatermark(
                role=RunnerCapacityRole.FAST,
                high_water_free_bytes=2 * 1024**3,
                critical_free_bytes=1024**3,
            ),
            RunnerCapacityWatermark(
                role=RunnerCapacityRole.DOCKER,
                high_water_free_bytes=15 * 1024**3,
                critical_free_bytes=10 * 1024**3,
            ),
            RunnerCapacityWatermark(
                role=RunnerCapacityRole.MACOS,
                high_water_free_bytes=12 * 1024**3,
                critical_free_bytes=8 * 1024**3,
            ),
            RunnerCapacityWatermark(
                role=RunnerCapacityRole.BACKTESTER,
                high_water_free_bytes=30 * 1024**3,
                critical_free_bytes=20 * 1024**3,
            ),
        ),
        tiers=tuple(
            SystemCapacityTier(
                tier_id=tier,
                kind="measurement_bound",
                status="UNKNOWN_DO_NOT_PRUNE",
                physical_free_disk=CapacityPhysicalBoundary(
                    status="UNKNOWN_DO_NOT_PRUNE",
                    high_water_free_bytes=None,
                    critical_free_bytes=None,
                ),
            )
            for tier in CapacityTierId
        ),
    )


def test_capacity_client_uses_fixed_route_and_reader_auth() -> None:
    snapshot = _snapshot()
    seen: list[tuple[str, dict[str, str], float]] = []

    def transport(url: str, headers: dict[str, str], timeout: float) -> tuple[int, bytes]:
        seen.append((url, headers, timeout))
        return 200, snapshot.model_dump_json().encode()

    client = SystemCapacityControlClient(
        ClientSettings(reader_token="reader-secret", timeout_seconds=1.25),
        transport=transport,
    )
    result = client.system_capacity()

    assert result == snapshot
    assert seen == [
        (
            "http://127.0.0.1:8765/api/control/v1/system/capacity",
            {"Accept": "application/json", "Authorization": "Bearer reader-secret"},
            1.25,
        )
    ]
    assert "reader-secret" not in repr(client)


def test_capacity_client_rejects_incompatible_response() -> None:
    client = SystemCapacityControlClient(
        transport=lambda _url, _headers, _timeout: (200, b'{"schema_version":1,"unexpected":true}')
    )

    with pytest.raises(ControlClientError) as captured:
        client.system_capacity()

    assert captured.value.code == "CONTROL_RESPONSE_INVALID"
    assert "unexpected" not in str(captured.value)


def test_system_capacity_cli_json_uses_typed_client(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = _snapshot()
    observed_settings: list[ClientSettings] = []

    class FakeCapacityClient:
        def __init__(self, settings: ClientSettings) -> None:
            observed_settings.append(settings)

        def system_capacity(self) -> SystemCapacitySnapshot:
            return snapshot

    monkeypatch.setenv("MODULAR_CONTROL_READER_TOKEN", "cli-reader-secret")
    monkeypatch.setattr(
        "modular_control_plane.cli_system.SystemCapacityControlClient",
        FakeCapacityClient,
    )

    exit_code = main(
        [
            "--url",
            "http://127.0.0.1:8765",
            "--timeout",
            "1.5",
            "system",
            "capacity",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["policy_status"] == "PARTIAL_MEASURED_FAIL_CLOSED"
    assert payload["global_local_disk_watermark_available"] is False
    assert observed_settings[0].reader_token == "cli-reader-secret"
    assert observed_settings[0].timeout_seconds == 1.5


def test_system_capacity_cli_human_output_never_claims_local_watermark(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    snapshot = _snapshot()

    class FakeCapacityClient:
        def __init__(self, _settings: ClientSettings) -> None:
            pass

        def system_capacity(self) -> SystemCapacitySnapshot:
            return snapshot

    monkeypatch.setattr(
        "modular_control_plane.cli_system.SystemCapacityControlClient",
        FakeCapacityClient,
    )

    assert main(["system", "capacity"]) == 0
    output = capsys.readouterr().out
    assert "global physical watermark unavailable" in output
    assert "high_free=UNKNOWN" in output
    assert "critical_free=UNKNOWN" in output
    assert "deletion=NONE" in output
    assert "time-to-full" not in output.lower()


def test_capacity_snapshot_rejects_duplicate_runner_role_and_tier() -> None:
    snapshot = _snapshot()
    data: dict[str, Any] = snapshot.model_dump(mode="json")
    data["runner_watermarks"][1] = data["runner_watermarks"][0]
    with pytest.raises(ValueError, match="unique by role"):
        SystemCapacitySnapshot.model_validate(data)

    data = snapshot.model_dump(mode="json")
    data["tiers"][1] = data["tiers"][0]
    with pytest.raises(ValueError, match="unique by tier_id"):
        SystemCapacitySnapshot.model_validate(data)
