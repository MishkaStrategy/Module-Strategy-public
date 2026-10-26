from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

POLICY_PATH = (
    Path(__file__).resolve().parents[1] / "docs/operations/STORAGE_PRESSURE_THRESHOLDS.json"
)

_STALE_PENDING_STATUSES = {
    "UNKNOWN_PENDING_NEAR_BOUNDARY_ADMISSION_AND_RECOVERY_PROOF",
    "UNKNOWN_PENDING_ADMISSION_HEADROOM_INTERPRETATION",
    "UNKNOWN_PENDING_OWNER_ADMISSION_AND_RECOVERY_HEADROOM",
}


def _policy() -> dict[str, Any]:
    value = json.loads(POLICY_PATH.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return cast(dict[str, Any], value)


def test_storage_pressure_inventory_records_accepted_owner_proofs_fail_closed() -> None:
    policy = _policy()
    tiers = policy["tiers"]
    assert isinstance(tiers, dict)

    acceptance = tiers["system_acceptance"]
    assert acceptance["status"] == "MEASURED_NEAR_BOUNDARY_PROVEN_THRESHOLD_UNKNOWN"
    assert acceptance["accepted_near_boundary_proof"]["pull_request"] == 591
    assert acceptance["physical_free_disk"]["high_water_free_bytes"] is None
    assert acceptance["physical_free_disk"]["critical_free_bytes"] is None
    assert (
        acceptance["physical_free_disk"]["status"]
        == "UNKNOWN_NO_OWNER_FREE_SPACE_ADMISSION_THRESHOLD"
    )
    assert acceptance["deletion_authority"] == "NONE"

    market_data = tiers["market_data_registry_storage"]
    assert market_data["status"] == "DYNAMIC_NEXT_PUBLICATION_ADMISSION_ACCEPTED"
    admission = market_data["accepted_publication_admission"]
    assert admission["pull_request"] == 600
    assert admission["formats"] == ["csv", "parquet"]
    assert admission["reject_before_first_version_write"] is True
    assert admission["registry_transaction_recovery_reserve_bytes"] == 24_576
    assert (
        market_data["physical_free_disk"]["status"]
        == "DYNAMIC_NEXT_PUBLICATION_ADMISSION_ACCEPTED_NO_GLOBAL_WATERMARK"
    )
    assert market_data["physical_free_disk"]["high_water_free_bytes"] is None
    assert market_data["physical_free_disk"]["critical_free_bytes"] is None
    assert market_data["deletion_authority"] == "NONE"

    control_plane = tiers["market_data_application_control_plane_storage"]
    assert control_plane["status"] == "MEASURED_NEAR_BOUNDARY_PROVEN_THRESHOLD_UNKNOWN"
    measurement = control_plane["accepted_measurement"]
    assert measurement["pull_request"] == 604
    assert measurement["accepted_run_id"] == 31_500_006_636
    assert measurement["matrix_python_versions"] == ["3.12", "3.13"]
    assert measurement["scales"] == [1, 100, 1000]
    assert measurement["repeats"] == 3
    assert measurement["component_maxima_are_independent_observations"] is True
    assert measurement["do_not_sum_component_maxima_into_synthetic_total"] is True
    assert (
        measurement["max_live_bytes_per_item_semantics"]
        == "TOTAL_LIVE_FOOTPRINT_DIVIDED_BY_SCALE_NOT_MARGINAL_GROWTH"
    )
    operational = measurement["operational_jobs_scale_1000"]
    assert operational["max_observed_live_total_bytes"] == 5_267_128
    security = measurement["security_token_audit_scale_1000"]
    assert security["max_observed_live_total_bytes"] == 4_525_608
    shared = measurement["shared_jobs_sqlite_scale_1000"]
    assert shared["durable_counts"] == {
        "jobs": 1000,
        "events": 2000,
        "leases": 0,
        "repair_confirmations": 1000,
    }
    assert shared["max_database_bytes"] == 1_724_416
    assert shared["max_wal_bytes"] == 4_169_472
    assert shared["max_observed_live_total_bytes"] == 5_914_344
    boundary = control_plane["accepted_near_boundary_proof"]
    assert boundary["issue"] == 603
    assert boundary["pull_request"] == 604
    assert boundary["fault"] == "SQLITE_FULL_VIA_BOUNDED_PRAGMA_MAX_PAGE_COUNT"
    assert (
        boundary["boundary_evidence_sha256"]
        == "3bdac93a5776851322d52b5b366af21de0e86f35b8aa1fc52b7ce482a64d2774"
    )
    assert boundary["integrity_reopen"] == "PASS"
    assert boundary["deletion_authority"] == "NONE"
    assert control_plane["physical_free_disk"]["high_water_free_bytes"] is None
    assert control_plane["physical_free_disk"]["critical_free_bytes"] is None
    assert (
        control_plane["physical_free_disk"]["status"]
        == "UNKNOWN_NO_OWNER_FREE_SPACE_ADMISSION_THRESHOLD"
    )
    assert control_plane["deletion_authority"] == "NONE"

    research = tiers["research_platform"]
    assert research["status"] == "MEASURED_NEAR_BOUNDARY_PROVEN_THRESHOLD_UNKNOWN"
    assert research["accepted_near_boundary_proofs"]["sqlite"]["pull_request"] == 593
    assert research["accepted_near_boundary_proofs"]["artifact_publication"]["pull_request"] == 596
    assert research["physical_free_disk"]["high_water_free_bytes"] is None
    assert research["physical_free_disk"]["critical_free_bytes"] is None
    assert (
        research["physical_free_disk"]["status"]
        == "UNKNOWN_NO_OWNER_FREE_SPACE_ADMISSION_THRESHOLD"
    )
    assert research["deletion_authority"] == "NONE"

    runtime = tiers["core_paper_trading_durable_state"]
    assert runtime["status"] == "UNKNOWN_DO_NOT_PRUNE"
    assert runtime["source_review"]["selected_full_rc_persistent_backend"] is None
    assert runtime["high_water_free_bytes"] is None
    assert runtime["critical_free_bytes"] is None
    assert runtime["deletion_authority"] == "NONE"

    adoption_rule = policy["adoption_rule"]
    assert isinstance(adoption_rule, dict)
    assert adoption_rule["unknown_is_not_deletion_permission"] is True


def test_storage_pressure_inventory_has_no_stale_pending_owner_proof_status() -> None:
    serialized = POLICY_PATH.read_text(encoding="utf-8")
    assert not (_STALE_PENDING_STATUSES & set(serialized.split('"')))


def test_storage_pressure_inventory_references_current_proof_files() -> None:
    policy = _policy()
    tiers = policy["tiers"]
    assert isinstance(tiers, dict)
    checked_sources = (
        *tiers["system_acceptance"]["sources"],
        *tiers["market_data_registry_storage"]["sources"][1:],
        *tiers["market_data_application_control_plane_storage"]["sources"],
        *tiers["research_platform"]["sources"][1:],
    )
    root = POLICY_PATH.parents[2]
    for source in checked_sources:
        assert (root / source).is_file(), source
