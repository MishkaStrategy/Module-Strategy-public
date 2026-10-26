from __future__ import annotations

from pathlib import Path

from market_data_application.storage_pressure_benchmark import (
    measure_control_plane_storage_boundary,
    measure_control_plane_storage_growth,
)


def _assert_sanitized_evidence(evidence: dict[str, object], tmp_path: Path) -> None:
    serialized = repr(evidence)
    assert str(tmp_path) not in serialized
    assert "hostname" not in serialized.lower()
    assert "audit-key" not in serialized.lower()
    assert "plaintext" not in serialized.lower()
    assert evidence["deletion_authority"] == "NONE"
    assert evidence["physical_high_water_bytes"] is None
    assert evidence["physical_critical_bytes"] is None


def test_control_plane_storage_growth_measures_both_concrete_sqlite_surfaces(
    tmp_path: Path,
) -> None:
    evidence = measure_control_plane_storage_growth(
        scales=(1, 8),
        repeats=2,
        work_root=tmp_path,
    )

    assert evidence["schema_version"] == 1
    assert evidence["owner"] == "market_data_control_plane"
    assert evidence["measurement"] == "sqlite_growth"
    assert evidence["scales"] == [1, 8]
    assert evidence["repeats"] == 2
    stores = evidence["stores"]
    assert isinstance(stores, dict)
    assert set(stores) == {"operational_jobs", "security_audit"}
    for store in stores.values():
        assert isinstance(store, dict)
        assert store["max_live_total_bytes"] > 0
        assert store["max_settled_database_bytes"] > 0
        assert store["max_wal_bytes"] >= 0
        assert store["max_shm_bytes"] >= 0
        assert store["integrity_reopen"] == "PASS"
        assert store["repeat_noise_bytes"] >= 0
    assert stores["operational_jobs"]["job_event_identity"] == "PASS"
    assert stores["operational_jobs"]["lease_lifecycle"] == "PASS"
    assert stores["security_audit"]["audit_chain"] == "PASS"
    assert stores["security_audit"]["token_generation_reopen"] == "PASS"
    assert evidence["watermark_status"] == "UNKNOWN_MEASUREMENT_AND_NEAR_BOUNDARY_PROOF_ONLY"
    _assert_sanitized_evidence(evidence, tmp_path)


def test_control_plane_storage_boundary_is_atomic_and_retryable(tmp_path: Path) -> None:
    evidence = measure_control_plane_storage_boundary(
        max_attempts=64,
        work_root=tmp_path,
    )

    assert evidence["schema_version"] == 1
    assert evidence["owner"] == "market_data_control_plane"
    assert evidence["measurement"] == "sqlite_near_boundary_recovery"
    assert evidence["boundary_mechanism"] == "sqlite_max_page_count"
    proofs = evidence["proofs"]
    assert isinstance(proofs, dict)
    assert set(proofs) == {"operational_jobs", "security_audit"}

    jobs = proofs["operational_jobs"]
    assert jobs["sqlite_full_observed"] is True
    assert jobs["failure_transaction_atomic"] is True
    assert jobs["failed_job_identity_absent"] is True
    assert jobs["failed_lease_identity_absent"] is True
    assert jobs["integrity_reopen"] == "PASS"
    assert jobs["same_request_retry"] == "PASS"
    assert jobs["idempotent_reuse"] == "PASS"

    security = proofs["security_audit"]
    assert security["sqlite_full_observed"] is True
    assert security["failure_transaction_atomic"] is True
    assert security["committed_generation_preserved"] is True
    assert security["audit_chain_preserved"] is True
    assert security["integrity_reopen"] == "PASS"
    assert security["capacity_restore_retry"] == "PASS"
    assert security["generation_compare_and_swap"] == "PASS"

    assert evidence["watermark_status"] == "UNKNOWN_MEASUREMENT_AND_NEAR_BOUNDARY_PROOF_ONLY"
    _assert_sanitized_evidence(evidence, tmp_path)
