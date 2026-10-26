from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_system_acceptance import (
    AcceptanceExecutorRegistry,
    AcceptanceMutation,
    AcceptanceMutationRequest,
    AcceptanceRunSpec,
    SQLiteAcceptanceStore,
    SystemAcceptanceOwner,
    TrustedOperator,
)
from modular_system_acceptance.contracts import AcceptanceErrorCode, AcceptanceOwnerError
from modular_system_acceptance.fault_recovery import (
    assert_recovered_state_matches,
    load_fault_recovery_matrix,
)

ROOT = Path(__file__).resolve().parents[1]
MATRIX_PATH = ROOT / "fault_recovery_matrix.v1.json"
NOW = datetime(2026, 8, 28, 1, 0, tzinfo=UTC)


def _pragma_int(connection: sqlite3.Connection, pragma: str) -> int:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    assert row is not None
    return int(row[0])


def _mutation_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM acceptance_mutations").fetchone()
    assert row is not None
    return int(row[0])


def _run_count(connection: sqlite3.Connection) -> int:
    row = connection.execute("SELECT COUNT(*) FROM acceptance_runs").fetchone()
    assert row is not None
    return int(row[0])


def _start_request() -> AcceptanceMutationRequest:
    return AcceptanceMutationRequest(
        request_id="fault-recovery-sqlite-start",
        operator=TrustedOperator("control-plane:local-operator"),
        mutation=AcceptanceMutation.START,
        run_id=None,
        spec=AcceptanceRunSpec(
            full_rc_sha="a" * 40,
            dataset_id="btc-usdtm-5m-v1",
            dataset_digest="b" * 64,
            strategy_id="strategy-candidate-v2",
            strategy_digest="c" * 64,
            config_digest="d" * 64,
        ),
    )


def _owner(path: Path, *, instance_id: str) -> tuple[SystemAcceptanceOwner, SQLiteAcceptanceStore]:
    store = SQLiteAcceptanceStore(path)
    return (
        SystemAcceptanceOwner(
            store,
            AcceptanceExecutorRegistry({}),
            instance_id=instance_id,
            clock=lambda: NOW,
            lease_seconds=5,
        ),
        store,
    )


def _factual_state(connection: sqlite3.Connection) -> dict[str, object]:
    row = connection.execute(
        "SELECT payload FROM acceptance_runs ORDER BY updated_at DESC, run_id DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    payload = str(row[0]).encode("utf-8")
    return {
        "acceptance_run_count": _run_count(connection),
        "acceptance_mutation_count": _mutation_count(connection),
        "acceptance_latest_revision": hashlib.sha256(payload).hexdigest(),
    }


def test_sqlite_full_write_rolls_back_and_reopens_with_integrity(tmp_path: Path) -> None:
    database = tmp_path / "acceptance.sqlite3"
    store = SQLiteAcceptanceStore(database)
    connection = store._db

    # Compact first so the allocation ceiling is deterministic: there must be no
    # reusable database pages that could let the fault-injection write fit anyway.
    connection.execute("VACUUM")
    assert _pragma_int(connection, "freelist_count") == 0
    page_count = _pragma_int(connection, "page_count")
    configured_limit = connection.execute(f"PRAGMA max_page_count={page_count}").fetchone()
    assert configured_limit == (page_count,)
    assert _mutation_count(connection) == 0

    # The oversized internal fingerprint is fault-injection payload only. It forces
    # SQLite to request additional pages without consuming host disk capacity.
    with pytest.raises(AcceptanceOwnerError) as captured:
        with store.transaction() as transaction:
            store.insert_mutation(
                request_id="storage-full-fault",
                fingerprint="f" * 200_000,
                created_at=datetime(2026, 1, 1, tzinfo=UTC),
                connection=transaction,
            )

    assert captured.value.code is AcceptanceErrorCode.STORAGE_FAILURE
    assert captured.value.request_id == "storage-full-fault"
    assert _mutation_count(connection) == 0
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    store.close()

    reopened = SQLiteAcceptanceStore(database)
    try:
        assert reopened._db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert _mutation_count(reopened._db) == 0
        assert reopened.latest_run() is None
    finally:
        reopened.close()


def test_sqlite_busy_wal_owner_recovery_matches_uninterrupted_state(tmp_path: Path) -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("acceptance_sqlite_busy_wal")
    request = _start_request()

    reference_path = tmp_path / "reference.sqlite3"
    reference_owner, reference_store = _owner(reference_path, instance_id="reference-owner")
    try:
        reference_result = reference_owner.mutate(request)
        continuous = _factual_state(reference_store._db)
        assert reference_result.replayed is False
        assert reference_store._db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert reference_store._db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        reference_store.close()

    recovered_path = tmp_path / "recovered.sqlite3"
    fault_owner, fault_store = _owner(recovered_path, instance_id="fault-owner")
    fault_store._db.execute("PRAGMA busy_timeout=50")
    locker = sqlite3.connect(recovered_path, timeout=0.05, isolation_level=None)
    try:
        locker.execute("PRAGMA busy_timeout=50")
        assert locker.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        locker.execute("BEGIN IMMEDIATE")
        assert Path(f"{recovered_path}-wal").exists()
        assert Path(f"{recovered_path}-shm").exists()

        with pytest.raises(sqlite3.OperationalError, match="locked"):
            fault_owner.mutate(request)

        assert _run_count(fault_store._db) == 0
        assert _mutation_count(fault_store._db) == 0
    finally:
        locker.rollback()
        locker.close()

    assert fault_store._db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    fault_store.close()

    recovered_owner, recovered_store = _owner(recovered_path, instance_id="recovered-owner")
    try:
        recovered_result = recovered_owner.mutate(request)
        recovered = _factual_state(recovered_store._db)
        left, right = assert_recovered_state_matches(
            continuous,
            recovered,
            fields=scenario.factual_state_fields,
        )

        assert left == right
        assert recovered_result.replayed is False
        assert recovered_result.acceptance_run_id == reference_result.acceptance_run_id
        assert _run_count(recovered_store._db) == 1
        assert _mutation_count(recovered_store._db) == 1
        assert recovered_store._db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert recovered_store._db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    finally:
        recovered_store.close()
