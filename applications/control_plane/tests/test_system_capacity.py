from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from modular_control_plane.system_capacity import (
    CapacityTierId,
    JsonSystemCapacityProvider,
    RunnerCapacityRole,
    system_capacity_service_from_environment,
)

NOW = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
POLICY_PATH = (
    Path(__file__).resolve().parents[3] / "docs/operations/STORAGE_PRESSURE_THRESHOLDS.json"
)
GIB = 1024**3


def _policy() -> dict[str, Any]:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def _write_policy(tmp_path: Path, value: dict[str, Any]) -> Path:
    target = tmp_path / "capacity-policy.json"
    target.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
    return target.resolve()


def test_canonical_policy_projects_only_fail_closed_operator_semantics() -> None:
    snapshot = JsonSystemCapacityProvider(POLICY_PATH.resolve(), clock=lambda: NOW).snapshot()

    assert snapshot.observed_at == NOW
    assert snapshot.source_schema_version == 2
    assert snapshot.policy_status == "PARTIAL_MEASURED_FAIL_CLOSED"
    assert snapshot.unknown_means == "NO_DESTRUCTIVE_OR_CAPACITY_CLAIM"
    assert snapshot.global_local_disk_watermark_available is False
    assert len(snapshot.source_digest_sha256) == 64

    runner_by_role = {item.role: item for item in snapshot.runner_watermarks}
    assert runner_by_role[RunnerCapacityRole.FAST].high_water_free_bytes == 2 * GIB
    assert runner_by_role[RunnerCapacityRole.FAST].critical_free_bytes == GIB
    assert runner_by_role[RunnerCapacityRole.DOCKER].high_water_free_bytes == 15 * GIB
    assert runner_by_role[RunnerCapacityRole.DOCKER].critical_free_bytes == 10 * GIB
    assert runner_by_role[RunnerCapacityRole.MACOS].high_water_free_bytes == 12 * GIB
    assert runner_by_role[RunnerCapacityRole.MACOS].critical_free_bytes == 8 * GIB
    assert runner_by_role[RunnerCapacityRole.BACKTESTER].high_water_free_bytes == 30 * GIB
    assert runner_by_role[RunnerCapacityRole.BACKTESTER].critical_free_bytes == 20 * GIB

    tiers = {item.tier_id: item for item in snapshot.tiers}
    acceptance = tiers[CapacityTierId.SYSTEM_ACCEPTANCE]
    assert acceptance.status == "MEASURED_NEAR_BOUNDARY_PROVEN_THRESHOLD_UNKNOWN"
    assert acceptance.physical_free_disk.high_water_free_bytes is None
    assert acceptance.physical_free_disk.critical_free_bytes is None
    assert acceptance.physical_free_disk.status == "UNKNOWN_NO_OWNER_FREE_SPACE_ADMISSION_THRESHOLD"
    assert acceptance.owner_hard_limit_bytes == 1_073_741_824
    assert acceptance.owner_payload_limit_bytes == 256_000
    assert acceptance.deletion_authority == "NONE"
    acceptance_measurements = {item.metric: item.value for item in acceptance.measurements}
    assert acceptance_measurements == {
        "live_total_at_1000_runs": 6_618_784.0,
        "settled_database_at_1000_runs": 2_600_960.0,
        "growth_per_run_at_1000_runs": 6_532.448,
    }

    registry = tiers[CapacityTierId.MARKET_DATA_REGISTRY]
    assert registry.status == "DYNAMIC_NEXT_PUBLICATION_ADMISSION_ACCEPTED"
    assert registry.next_publication_admission is True
    assert registry.recovery_reserve_bytes == 24_576
    assert registry.physical_free_disk.high_water_free_bytes is None
    assert registry.physical_free_disk.critical_free_bytes is None
    assert (
        registry.physical_free_disk.status
        == "DYNAMIC_NEXT_PUBLICATION_ADMISSION_ACCEPTED_NO_GLOBAL_WATERMARK"
    )
    assert registry.deletion_authority == "NONE"

    application = tiers[CapacityTierId.MARKET_DATA_APPLICATION]
    assert application.measurements_aggregatable is False
    application_measurements = {item.metric: item.value for item in application.measurements}
    assert application_measurements == {
        "operational_live_total_scale_1000": 5_267_128.0,
        "security_live_total_scale_1000": 4_525_608.0,
        "shared_jobs_live_total_scale_1000": 5_914_344.0,
    }
    assert application.physical_free_disk.high_water_free_bytes is None
    assert application.deletion_authority == "NONE"

    research = tiers[CapacityTierId.RESEARCH_PLATFORM]
    assert research.physical_free_disk.high_water_free_bytes is None
    assert research.physical_free_disk.critical_free_bytes is None
    assert research.deletion_authority == "NONE"

    runtime = tiers[CapacityTierId.CORE_PAPER_TRADING]
    assert runtime.status == "UNKNOWN_DO_NOT_PRUNE"
    assert runtime.physical_free_disk.high_water_free_bytes is None
    assert runtime.physical_free_disk.critical_free_bytes is None
    assert runtime.measurements == ()
    assert runtime.deletion_authority == "NONE"

    rendered = snapshot.model_dump_json()
    assert "source_review" not in rendered
    assert "sources" not in rendered
    assert "rationale" not in rendered
    assert "/Users/" not in rendered
    assert "selected_full_rc_persistent_backend" not in rendered


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (
            lambda policy: policy["policy"].__setitem__("forecast_owner", "second_monitor"),
            "forecast owner",
        ),
        (
            lambda policy: policy["policy"].__setitem__("no_duplicate_monitor", False),
            "duplicate monitors",
        ),
        (
            lambda policy: policy["adoption_rule"].__setitem__(
                "unknown_is_not_deletion_permission", False
            ),
            "deletion permission",
        ),
        (
            lambda policy: policy["tiers"]["system_acceptance"].__setitem__(
                "deletion_authority", "AUTO"
            ),
            "deletion authority",
        ),
        (
            lambda policy: policy["tiers"]["market_data_application_control_plane_storage"][
                "accepted_measurement"
            ].__setitem__("do_not_sum_component_maxima_into_synthetic_total", False),
            "non-aggregatable",
        ),
        (
            lambda policy: policy["tiers"]["system_acceptance"].__setitem__(
                "status", "UNKNOWN_PENDING_OWNER_ADMISSION_AND_RECOVERY_HEADROOM"
            ),
            "stale pending",
        ),
    ],
)
def test_policy_tampering_fails_closed(
    tmp_path: Path,
    mutation: Any,
    message: str,
) -> None:
    policy = _policy()
    mutation(policy)
    path = _write_policy(tmp_path, policy)

    with pytest.raises(ValueError, match=message):
        JsonSystemCapacityProvider(path, clock=lambda: NOW).snapshot()


def test_physical_thresholds_cannot_be_partially_invented(tmp_path: Path) -> None:
    policy = _policy()
    physical = policy["tiers"]["system_acceptance"]["physical_free_disk"]
    physical["high_water_free_bytes"] = 10_000

    with pytest.raises(ValueError, match="both known or both unknown"):
        JsonSystemCapacityProvider(_write_policy(tmp_path, policy), clock=lambda: NOW).snapshot()


def test_environment_attachment_is_explicit_absolute_and_optional(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", raising=False)
    assert system_capacity_service_from_environment(clock=lambda: NOW) is None

    monkeypatch.setenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", "relative/policy.json")
    with pytest.raises(ValueError, match="absolute"):
        system_capacity_service_from_environment(clock=lambda: NOW)

    monkeypatch.setenv("MODULAR_CONTROL_STORAGE_PRESSURE_POLICY_PATH", str(POLICY_PATH.resolve()))
    service = system_capacity_service_from_environment(clock=lambda: NOW)
    assert service is not None
    assert service.capacity().policy_status == "PARTIAL_MEASURED_FAIL_CLOSED"


def test_provider_rejects_naive_clock_and_oversized_policy(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        JsonSystemCapacityProvider(
            POLICY_PATH.resolve(), clock=lambda: datetime(2026, 8, 12)
        ).snapshot()

    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"{" + b" " * (512 * 1024) + b"}")
    with pytest.raises(ValueError, match="maximum size"):
        JsonSystemCapacityProvider(oversized.resolve(), clock=lambda: NOW).snapshot()


def test_provider_refuses_symlink_policy_when_platform_supports_no_follow(tmp_path: Path) -> None:
    if not hasattr(os, "O_NOFOLLOW"):
        pytest.skip("O_NOFOLLOW unavailable")
    link = tmp_path / "policy-link.json"
    try:
        link.symlink_to(POLICY_PATH)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable")

    with pytest.raises(OSError):
        JsonSystemCapacityProvider(Path(os.path.abspath(link)), clock=lambda: NOW).snapshot()
