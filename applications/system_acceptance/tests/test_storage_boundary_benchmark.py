from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_system_acceptance.contracts import AcceptanceErrorCode, AcceptanceOwnerError
from modular_system_acceptance.storage import SQLiteAcceptanceStore
from modular_system_acceptance.storage_boundary_benchmark import (
    measure_storage_boundary_recovery,
)


def test_near_boundary_storage_failure_is_atomic_and_recoverable(tmp_path: Path) -> None:
    payload = measure_storage_boundary_recovery(
        max_attempts=64,
        work_root=tmp_path / "boundary-case",
    )

    assert payload["schema_version"] == 1
    assert payload["owner"] == "system_acceptance"
    assert payload["measurement"] == "sqlite_near_boundary_transaction_recovery"
    assert payload["boundary_mechanism"] == "sqlite_max_page_count"
    assert payload["watermark_status"] == "UNKNOWN_NEAR_BOUNDARY_PROOF_ONLY"
    assert payload["physical_high_water_bytes"] is None
    assert payload["physical_critical_bytes"] is None
    assert payload["deletion_authority"] == "NONE"

    successful_before_full = payload["successful_terminal_runs_before_full"]
    assert isinstance(successful_before_full, int)
    assert 0 <= successful_before_full < 64

    failure = payload["failure"]
    assert failure["code"] == "STORAGE_FAILURE"
    assert failure["mutation"] in {"START", "CANCEL"}
    assert failure["in_progress_mutations_after_failure"] == 0
    assert failure["integrity_check_after_failure"] == "ok"
    assert "database or disk is full" not in failure["sanitized_message"].lower()

    persisted = failure["failed_request_persisted"]
    assert isinstance(persisted, bool)
    restored = payload["after_capacity_restore"]
    assert restored["terminal_status"] == "CANCELLED"
    assert restored["integrity_check"] == "ok"
    assert restored["reopen_status"] == "CANCELLED"
    assert restored["recovery_request_committed_once"] is True
    assert restored["recovery_request_final_state"] == "SUCCEEDED"
    assert restored["in_progress_mutations"] == 0

    if persisted:
        assert failure["durable_state"] == "FAILED"
        assert failure["durable_error_code"] == "STORAGE_FAILURE"
        assert restored["recovery_mode"] == "NEW_REQUEST_AFTER_DURABLE_FAILURE"
        assert restored["same_request_retry_succeeded"] is False
        assert restored["durable_failure_replayed"] is True
        assert restored["failed_request_final_state"] == "FAILED"
    else:
        assert failure["durable_state"] is None
        assert failure["durable_error_code"] is None
        assert restored["recovery_mode"] == "SAME_REQUEST_RETRY"
        assert restored["same_request_retry_succeeded"] is True
        assert restored["durable_failure_replayed"] is False
        assert restored["failed_request_final_state"] == "SUCCEEDED"

    assert restored["row_counts"] == {
        "runs": successful_before_full + 1,
        "mutations": (successful_before_full + 1) * 2 + (1 if persisted else 0),
        "audit": (successful_before_full + 1) * 2 + (2 if persisted else 0),
    }


def test_active_transaction_missing_storage_claim_still_requires_recovery(
    tmp_path: Path,
) -> None:
    store = SQLiteAcceptanceStore(tmp_path / "active-missing.sqlite3")
    try:
        with pytest.raises(AcceptanceOwnerError) as caught:
            with store.transaction() as connection:
                assert connection.in_transaction is True
                store.finish_mutation(
                    request_id="missing-storage-claim",
                    result=None,
                    error_code=AcceptanceErrorCode.STORAGE_FAILURE,
                    completed_at=datetime(2026, 1, 1, tzinfo=UTC),
                    connection=connection,
                )
        assert caught.value.code is AcceptanceErrorCode.RECOVERY_REQUIRED
    finally:
        store.close()


def test_storage_boundary_measurement_is_sanitized_and_bounded(tmp_path: Path) -> None:
    payload = measure_storage_boundary_recovery(
        max_attempts=32,
        work_root=tmp_path / "sanitized-case",
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).lower()

    assert payload["max_attempts"] == 32
    assert payload["constrained_max_page_count"] > payload["initial_page_count"]
    assert payload["page_size_bytes"] > 0
    assert "hostname" not in encoded
    assert "private/" not in encoded
    assert "token=" not in encoded
    assert str(tmp_path).lower() not in encoded


@pytest.mark.parametrize("value", [0, -1, 257, True])
def test_storage_boundary_measurement_rejects_unbounded_attempts(
    tmp_path: Path,
    value: object,
) -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        measure_storage_boundary_recovery(
            max_attempts=value,  # type: ignore[arg-type]
            work_root=tmp_path / "invalid-case",
        )
