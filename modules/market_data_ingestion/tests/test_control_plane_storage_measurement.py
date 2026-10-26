from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

from market_data_application.storage_pressure_benchmark import (
    measure_control_plane_storage_boundary,
    measure_control_plane_storage_growth,
)

_SCALES = (1, 100, 1000)
_REPEATS = 3


def _store_scale_summary(store: dict[str, object], scale: int) -> dict[str, object]:
    live_total = store["max_live_total_bytes"]
    if isinstance(live_total, bool) or not isinstance(live_total, int):
        raise AssertionError("live total footprint must be an integer")
    return {
        "live_database_bytes": store["max_live_database_bytes"],
        "live_wal_bytes": store["max_wal_bytes"],
        "live_shm_bytes": store["max_shm_bytes"],
        "live_total_bytes": live_total,
        "settled_database_bytes": store["max_settled_database_bytes"],
        "repeat_total_range_bytes": store["repeat_noise_bytes"],
        "max_live_bytes_per_item": (live_total + scale - 1) // scale,
    }


def _representative_growth(tmp_path: Path) -> dict[str, object]:
    measurements: list[dict[str, object]] = []
    measurement_digests: list[str] = []
    for scale in _SCALES:
        growth = measure_control_plane_storage_growth(
            scales=(scale,),
            repeats=_REPEATS,
            work_root=tmp_path / f"scale-{scale}",
        )
        stores = growth["stores"]
        assert isinstance(stores, dict)
        operational = stores["operational_jobs"]
        security = stores["security_audit"]
        assert isinstance(operational, dict)
        assert isinstance(security, dict)
        digest = growth["evidence_sha256"]
        assert isinstance(digest, str)
        measurement_digests.append(digest)
        measurements.append(
            {
                "scale": scale,
                "operational_jobs": _store_scale_summary(operational, scale),
                "security_audit": _store_scale_summary(security, scale),
                "measurement_sha256": digest,
            }
        )
    payload: dict[str, object] = {
        "schema_version": 1,
        "owner": "market_data_control_plane",
        "measurement": "sqlite_representative_growth",
        "scales": list(_SCALES),
        "repeats": _REPEATS,
        "measurements": measurements,
        "measurement_sha256": measurement_digests,
        "physical_high_water_bytes": None,
        "physical_critical_bytes": None,
        "watermark_status": "UNKNOWN_MEASUREMENT_AND_NEAR_BOUNDARY_PROOF_ONLY",
        "deletion_authority": "NONE",
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    payload["evidence_sha256"] = hashlib.sha256(canonical.encode()).hexdigest()
    return payload


def test_control_plane_storage_representative_evidence(tmp_path: Path, capfd) -> None:
    growth = _representative_growth(tmp_path / "growth")
    boundary = measure_control_plane_storage_boundary(
        max_attempts=64,
        work_root=tmp_path / "boundary",
    )
    evidence = {
        "schema_version": 1,
        "owner": "market_data_control_plane",
        "growth": growth,
        "boundary": boundary,
    }

    assert growth["scales"] == [1, 100, 1000]
    assert growth["repeats"] == 3
    assert growth["physical_high_water_bytes"] is None
    assert growth["physical_critical_bytes"] is None
    assert growth["deletion_authority"] == "NONE"
    measurements = growth["measurements"]
    assert isinstance(measurements, list)
    assert [sample["scale"] for sample in measurements] == [1, 100, 1000]
    for sample in measurements:
        assert isinstance(sample, dict)
        scale = sample["scale"]
        assert isinstance(scale, int)
        for store_name in ("operational_jobs", "security_audit"):
            store = sample[store_name]
            assert isinstance(store, dict)
            assert store["live_database_bytes"] > 0
            assert store["live_wal_bytes"] >= 0
            assert store["live_shm_bytes"] >= 0
            assert store["live_total_bytes"] > 0
            assert store["settled_database_bytes"] > 0
            assert store["repeat_total_range_bytes"] >= 0
            assert store["max_live_bytes_per_item"] > 0
        assert isinstance(sample["measurement_sha256"], str)
    assert boundary["physical_high_water_bytes"] is None
    assert boundary["physical_critical_bytes"] is None
    assert boundary["deletion_authority"] == "NONE"

    serialized = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    assert str(tmp_path) not in serialized
    assert "hostname" not in serialized.lower()
    assert "audit-key" not in serialized.lower()
    assert "benchmark-token" not in serialized.lower()

    with capfd.disabled():
        print(f"MARKET_DATA_CONTROL_PLANE_STORAGE_EVIDENCE={serialized}", flush=True)

    reports = Path(__file__).resolve().parents[1] / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    (reports / f"market-data-control-plane-storage-py{version}.json").write_text(
        serialized + "\n",
        encoding="utf-8",
    )
