from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_application.contracts import JobOperation
from market_data_application.job_store import SQLiteJobStore
from market_data_application.lease_store import SQLiteJobLeaseStore
from market_data_application.repair_confirmation_store import SQLiteRepairConfirmationStore
from market_data_ingestion.enums import DataType, RunState

_SCALES = (1, 100, 1000)
_REPEATS = 3
_BASE_TIME = datetime(2026, 1, 1, tzinfo=UTC)


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _footprint(path: Path) -> dict[str, int]:
    def size(candidate: Path) -> int:
        return candidate.stat().st_size if candidate.exists() else 0

    database = size(path)
    wal = size(Path(f"{path}-wal"))
    shm = size(Path(f"{path}-shm"))
    return {
        "database_bytes": database,
        "wal_bytes": wal,
        "shm_bytes": shm,
        "total_bytes": database + wal + shm,
    }


def _settle(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        connection.close()
    return _footprint(path)


def _count(connection: sqlite3.Connection, query: str) -> int:
    row = connection.execute(query).fetchone()
    assert row is not None
    value = row[0]
    assert not isinstance(value, bool)
    assert isinstance(value, int)
    return value


def _durable_counts(path: Path) -> dict[str, int]:
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=30)
    try:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        return {
            "jobs": _count(connection, "SELECT COUNT(*) FROM jobs"),
            "events": _count(connection, "SELECT COUNT(*) FROM job_events"),
            "leases": _count(connection, "SELECT COUNT(*) FROM job_leases"),
            "repair_confirmations": _count(
                connection,
                "SELECT COUNT(*) FROM repair_confirmations",
            ),
        }
    finally:
        connection.close()


def _exercise_shared_jobs_database(
    path: Path,
    *,
    scale: int,
) -> tuple[dict[str, int], dict[str, int], dict[str, int]]:
    token_sequence = 0

    def token_factory(_size: int) -> str:
        nonlocal token_sequence
        token_sequence += 1
        return f"repair-confirmation-token-{token_sequence:016d}"

    jobs = SQLiteJobStore(path)
    leases = SQLiteJobLeaseStore(path)
    confirmations = SQLiteRepairConfirmationStore(path, token_factory=token_factory)
    keeper = sqlite3.connect(path, timeout=30)
    job_ids: list[str] = []
    confirmation_ids: list[str] = []
    try:
        keeper.execute("PRAGMA journal_mode=WAL")
        for index in range(scale):
            at = _BASE_TIME + timedelta(minutes=index)
            created = jobs.create_job(
                operation=JobOperation.DOWNLOAD,
                request={"symbol": "BTCUSDT", "sequence": index},
                idempotency_key=f"shared-jobs-growth-{index:06d}",
            )
            job_ids.append(created.job.job_id)
            lease = leases.claim(
                created.job.job_id,
                owner_id="shared-jobs-storage-worker",
                ttl_seconds=30,
                at=at,
            )
            assert lease is not None
            renewed = leases.renew(lease, ttl_seconds=30, at=at + timedelta(seconds=1))
            assert renewed is not None
            assert leases.release(renewed)
            jobs.transition(created.job.job_id, RunState.CANCELLED)

            preview_id = f"repair-preview:shared-jobs-{index:06d}"
            issued = confirmations.issue(
                dataset_id=f"shared-jobs-dataset-{index:06d}",
                version=1,
                preview_id=preview_id,
                action="gap_range_repair",
                selected_data_type=DataType.KLINES,
                acknowledged_mutations=("replace_normalized_partition",),
                actor_id="shared-jobs-storage-admin",
                ttl_seconds=60,
                now=at,
            )
            confirmation_ids.append(issued.record.confirmation_id)
            consumed = confirmations.consume(
                issued.record.confirmation_id,
                token=issued.token,
                actor_id="shared-jobs-storage-admin",
                preview_id=preview_id,
                now=at + timedelta(seconds=2),
            )
            assert consumed.consumed_at is not None
        live = _footprint(path)
    finally:
        keeper.close()

    counts = _durable_counts(path)
    assert counts == {
        "jobs": scale,
        "events": scale * 2,
        "leases": 0,
        "repair_confirmations": scale,
    }

    reopened_jobs = SQLiteJobStore(path)
    reopened_confirmations = SQLiteRepairConfirmationStore(
        path,
        token_factory=lambda _size: "unused-reopen-token-0000000000000000",
    )
    for job_id in job_ids:
        reopened_jobs.get_job(job_id)
        assert len(reopened_jobs.events_after(job_id)) == 2
    for confirmation_id in confirmation_ids:
        assert reopened_confirmations.get(confirmation_id).consumed_at is not None

    return live, _settle(path), counts


def _sample_int(sample: dict[str, object], section: str, field: str) -> int:
    nested = sample[section]
    assert isinstance(nested, dict)
    value = nested[field]
    assert not isinstance(value, bool)
    assert isinstance(value, int)
    return value


def _representative_shared_jobs_growth(tmp_path: Path) -> dict[str, object]:
    measurements: list[dict[str, object]] = []
    for scale in _SCALES:
        samples: list[dict[str, object]] = []
        live_totals: list[int] = []
        for repeat in range(_REPEATS):
            sample_root = tmp_path / f"scale-{scale}-repeat-{repeat}"
            sample_root.mkdir(parents=True, exist_ok=True)
            live, settled, counts = _exercise_shared_jobs_database(
                sample_root / "jobs.sqlite3",
                scale=scale,
            )
            live_totals.append(live["total_bytes"])
            sample: dict[str, object] = {
                "live": live,
                "settled": settled,
                "durable_counts": counts,
            }
            sample["sample_sha256"] = _canonical_sha256(sample)
            samples.append(sample)

        max_live_database = max(_sample_int(sample, "live", "database_bytes") for sample in samples)
        max_live_wal = max(_sample_int(sample, "live", "wal_bytes") for sample in samples)
        max_live_shm = max(_sample_int(sample, "live", "shm_bytes") for sample in samples)
        max_live_total = max(live_totals)
        max_settled_database = max(
            _sample_int(sample, "settled", "database_bytes") for sample in samples
        )
        measurements.append(
            {
                "scale": scale,
                "max_live_database_bytes": max_live_database,
                "max_live_wal_bytes": max_live_wal,
                "max_live_shm_bytes": max_live_shm,
                "max_live_total_bytes": max_live_total,
                "max_settled_database_bytes": max_settled_database,
                "repeat_total_range_bytes": max(live_totals) - min(live_totals),
                "max_live_bytes_per_item": (max_live_total + scale - 1) // scale,
                "durable_counts": {
                    "jobs": scale,
                    "events": scale * 2,
                    "leases": 0,
                    "repair_confirmations": scale,
                },
                "sample_sha256": [sample["sample_sha256"] for sample in samples],
            }
        )

    evidence: dict[str, object] = {
        "schema_version": 1,
        "owner": "market_data_operational_jobs_shared_sqlite",
        "measurement": "jobs_events_leases_repair_confirmations_growth",
        "scales": list(_SCALES),
        "repeats": _REPEATS,
        "measurements": measurements,
        "integrity_reopen": "PASS",
        "job_event_identity": "PASS",
        "lease_lifecycle": "PASS",
        "repair_confirmation_lifecycle": "PASS",
        "physical_high_water_bytes": None,
        "physical_critical_bytes": None,
        "watermark_status": "UNKNOWN_MEASUREMENT_AND_NEAR_BOUNDARY_PROOF_ONLY",
        "deletion_authority": "NONE",
    }
    evidence["evidence_sha256"] = _canonical_sha256(evidence)
    return evidence


def _measurement_int(measurement: dict[str, object], field: str) -> int:
    value = measurement[field]
    assert not isinstance(value, bool)
    assert isinstance(value, int)
    return value


def test_shared_jobs_storage_measurement_includes_repair_confirmations(
    tmp_path: Path, capfd
) -> None:
    evidence = _representative_shared_jobs_growth(tmp_path)

    assert evidence["scales"] == [1, 100, 1000]
    assert evidence["repeats"] == 3
    assert evidence["repair_confirmation_lifecycle"] == "PASS"
    assert evidence["physical_high_water_bytes"] is None
    assert evidence["physical_critical_bytes"] is None
    assert evidence["deletion_authority"] == "NONE"
    measurements = evidence["measurements"]
    assert isinstance(measurements, list)
    for measurement in measurements:
        assert isinstance(measurement, dict)
        scale = _measurement_int(measurement, "scale")
        counts = measurement["durable_counts"]
        assert counts == {
            "jobs": scale,
            "events": scale * 2,
            "leases": 0,
            "repair_confirmations": scale,
        }
        assert _measurement_int(measurement, "max_live_database_bytes") > 0
        assert _measurement_int(measurement, "max_live_total_bytes") > 0
        assert _measurement_int(measurement, "max_settled_database_bytes") > 0
        assert _measurement_int(measurement, "repeat_total_range_bytes") >= 0
        assert _measurement_int(measurement, "max_live_bytes_per_item") > 0

    serialized = json.dumps(evidence, sort_keys=True, separators=(",", ":"))
    assert str(tmp_path) not in serialized
    assert "hostname" not in serialized.lower()
    assert "repair-confirmation-token" not in serialized.lower()
    assert "shared-jobs-storage-admin" not in serialized.lower()

    with capfd.disabled():
        print(f"MARKET_DATA_SHARED_JOBS_STORAGE_EVIDENCE={serialized}", flush=True)

    reports = Path(__file__).resolve().parents[1] / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    version = f"{sys.version_info.major}.{sys.version_info.minor}"
    (reports / f"shared-jobs-storage-pressure-py{version}.json").write_text(
        serialized + "\n",
        encoding="utf-8",
    )
