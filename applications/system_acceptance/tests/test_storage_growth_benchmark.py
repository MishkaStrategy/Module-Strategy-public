from __future__ import annotations

import json
from pathlib import Path

import pytest

from modular_system_acceptance.storage_growth_benchmark import (
    _regular_file_bytes,
    measure_storage_growth,
)


def test_storage_growth_benchmark_uses_durable_owner_workload_without_watermark_claim(
    tmp_path: Path,
) -> None:
    result = measure_storage_growth(scales=(1, 3), repeats=1, work_root=tmp_path)

    assert result["owner"] == "system_acceptance"
    assert result["measurement"] == "sqlite_owner_storage_growth"
    assert result["workload"] == "public_owner_start_then_cancel_plus_restart_recovery"
    assert result["scales"] == [1, 3]
    assert result["repeats"] == 1
    assert result["physical_high_water_bytes"] is None
    assert result["physical_critical_bytes"] is None
    assert result["watermark_status"] == "UNKNOWN_MEASUREMENT_ONLY"
    assert result["deletion_authority"] == "NONE"

    cases = result["cases"]
    assert isinstance(cases, list) and len(cases) == 1
    case = cases[0]
    assert case["row_counts"] == {"runs": 3, "mutations": 6, "audit": 6}
    assert case["integrity_check"] == "ok"
    assert case["reopen_status"] == "CANCELLED"

    checkpoints = case["checkpoints"]
    assert [checkpoint["runs"] for checkpoint in checkpoints] == [1, 3]
    for checkpoint in checkpoints:
        footprint = checkpoint["footprint"]
        assert footprint["database_bytes"] > 0
        assert footprint["wal_bytes"] >= 0
        assert footprint["shm_bytes"] >= 0
        assert footprint["total_bytes"] == (
            footprint["database_bytes"] + footprint["wal_bytes"] + footprint["shm_bytes"]
        )
        assert checkpoint["growth_bytes_from_initialized_store"] >= 0
        assert checkpoint["growth_bytes_per_run"] >= 0.0

    recovery = result["recovery_smoke"]
    assert recovery["started_status"] == "QUEUED"
    assert recovery["recovered_status"] == "RECOVERING"
    assert recovery["terminal_status"] == "CANCELLED"
    assert recovery["row_counts"] == {"runs": 1, "mutations": 2, "audit": 2}
    assert recovery["integrity_check"] == "ok"
    assert recovery["measured_recovery_increment_bytes"] >= 0
    assert recovery["measured_recovery_and_terminalization_peak_increment_bytes"] >= 0

    encoded = json.dumps(result, sort_keys=True)
    assert str(tmp_path) not in encoded
    assert "hostname" not in encoded
    assert "path" not in encoded


def test_storage_growth_benchmark_rejects_unsafe_or_nondeterministic_bounds(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="strictly increasing"):
        measure_storage_growth(scales=(2, 1), repeats=1, work_root=tmp_path / "descending")
    with pytest.raises(ValueError, match="within 1..10000"):
        measure_storage_growth(scales=(10_001,), repeats=1, work_root=tmp_path / "oversized")
    with pytest.raises(ValueError, match="repeats"):
        measure_storage_growth(scales=(1,), repeats=0, work_root=tmp_path / "no-repeats")


def test_storage_growth_component_measurement_refuses_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"1234")
    link = tmp_path / "component"
    link.symlink_to(target)

    with pytest.raises(RuntimeError, match="non-regular SQLite component"):
        _regular_file_bytes(link)
