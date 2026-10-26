from __future__ import annotations

import hashlib
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from modular_results_registry import (
    AuditEvidenceRecord,
    EvidenceStatus,
    ExperimentRecord,
    ExperimentStatus,
    RegistrationPackage,
    SQLiteRegistry,
    Severity,
)
from modular_results_registry.evidence_query import (
    ReadOnlyEvidenceNotFound,
    ReadOnlyEvidenceRegistry,
    ReadOnlyEvidenceRegistryError,
)

NOW = datetime(2026, 8, 7, 13, 15, tzinfo=UTC)


def _seed(path: Path) -> AuditEvidenceRecord:
    experiment = ExperimentRecord(
        experiment_id="experiment-1",
        research_id="research-1",
        title="Read-only evidence fixture",
        description="Owner-valid Results Aggregator evidence seed",
        strategy_id="strategy-1",
        strategy_version="1.0.0",
        commit_sha="6c8a636700143e027eccd480c675f28ebd023cb6",
        branch="control-ui/evidence-test",
        working_tree_status="CLEAN",
        diff_hash=None,
        patch_artifact_id=None,
        module_versions={"results_aggregator": "0.1.0.dev0"},
        dependency_lock_hash="d" * 64,
        started_at=NOW,
        status=ExperimentStatus.COMPLETED,
        search_method="fixed",
        split_policy="holdout",
        completed_at=NOW,
        tags=("read-only-evidence",),
    )
    record = AuditEvidenceRecord(
        evidence_id="evidence-1",
        experiment_id=experiment.experiment_id,
        run_ids=(),
        evidence_type="robustness-summary",
        producer="results-aggregator",
        producer_version="1.0",
        status=EvidenceStatus.PASS,
        severity=Severity.HIGH,
        artifact_ids=(),
        created_at=NOW,
        scope={"private_path": "/Users/private/results", "window": "holdout"},
        evidence_hash="a" * 64,
        limitations=("private limitation detail",),
    )
    package = RegistrationPackage(
        package_id="package-evidence-readonly",
        producer_module_id="results-readonly-test",
        producer_module_version="1",
        artifacts=(),
        datasets=(),
        configurations=(),
        experiments=(experiment,),
        runs=(),
        metric_definitions=(),
        metrics=(),
        evidence=(record,),
        roles=(),
    )
    with SQLiteRegistry(path) as registry:
        outcome = registry.register_package(package)
        assert outcome.state == "REGISTERED"
    return record


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fault_inject(path: Path, statement: str, parameters: tuple[object, ...] = ()) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute(statement, parameters)
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
        if mode is None or str(mode[0]).lower() != "delete":
            raise AssertionError("fault injection failed to restore quiescent SQLite journal mode")


def test_read_only_evidence_registry_projects_safe_metadata_without_mutation(
    tmp_path: Path,
) -> None:
    path = tmp_path / "results.sqlite3"
    record = _seed(path)
    before_hash = _sha(path)
    before_mtime = path.stat().st_mtime_ns
    before_entries = tuple(sorted(item.name for item in tmp_path.iterdir()))

    reader = ReadOnlyEvidenceRegistry(path)
    integrity = reader.verify_integrity()
    snapshots = reader.list_evidence()
    detail = reader.get_evidence(record.evidence_id)

    assert integrity.schema_version == 4
    assert integrity.registry_revision == 1
    assert integrity.evidence_count == 1
    assert integrity.evidence_rows_verified is True
    assert snapshots == (detail,)
    assert detail.evidence_id == "evidence-1"
    assert detail.experiment_id == "experiment-1"
    assert detail.status is EvidenceStatus.PASS
    assert detail.severity is Severity.HIGH
    assert detail.run_count == 0
    assert detail.artifact_count == 0
    assert detail.limitations_count == 1
    assert detail.evidence_hash == "a" * 64

    rendered = repr(detail)
    assert "/Users/private/results" not in rendered
    assert "private limitation detail" not in rendered
    assert "private_path" not in rendered

    assert _sha(path) == before_hash
    assert path.stat().st_mtime_ns == before_mtime
    assert tuple(sorted(item.name for item in tmp_path.iterdir())) == before_entries


def test_read_only_evidence_registry_missing_path_is_side_effect_free(tmp_path: Path) -> None:
    path = tmp_path / "missing" / "results.sqlite3"

    with pytest.raises(ReadOnlyEvidenceRegistryError, match="does not exist"):
        ReadOnlyEvidenceRegistry(path)

    assert not path.exists()
    assert not path.parent.exists()


def test_read_only_evidence_registry_rejects_symlink(tmp_path: Path) -> None:
    path = tmp_path / "results.sqlite3"
    _seed(path)
    linked = tmp_path / "linked.sqlite3"
    linked.symlink_to(path)

    with pytest.raises(ReadOnlyEvidenceRegistryError, match="symlink"):
        ReadOnlyEvidenceRegistry(linked)


def test_read_only_evidence_registry_rejects_active_sqlite_sidecars(tmp_path: Path) -> None:
    path = tmp_path / "results.sqlite3"
    _seed(path)
    sidecar = Path(str(path) + "-wal")
    sidecar.write_bytes(b"active-wal-canary")

    with pytest.raises(ReadOnlyEvidenceRegistryError, match="not quiescent"):
        ReadOnlyEvidenceRegistry(path)

    assert sidecar.read_bytes() == b"active-wal-canary"


def test_read_only_evidence_registry_fails_closed_on_payload_tamper(tmp_path: Path) -> None:
    path = tmp_path / "results.sqlite3"
    _seed(path)
    _fault_inject(path, "DROP TRIGGER prevent_evidence_update")
    _fault_inject(
        path,
        "UPDATE evidence SET payload_json=? WHERE evidence_id='evidence-1'",
        (b'{"tampered":true}',),
    )

    with pytest.raises(ReadOnlyEvidenceRegistryError, match="content hash mismatch"):
        ReadOnlyEvidenceRegistry(path)


def test_read_only_evidence_registry_normalizes_invalid_schema_metadata(tmp_path: Path) -> None:
    path = tmp_path / "results.sqlite3"
    _seed(path)
    _fault_inject(
        path,
        "UPDATE schema_meta SET value='04' WHERE key='schema_version'",
    )

    with pytest.raises(ReadOnlyEvidenceRegistryError, match="schema version is invalid"):
        ReadOnlyEvidenceRegistry(path)


def test_read_only_evidence_registry_returns_typed_not_found(tmp_path: Path) -> None:
    path = tmp_path / "results.sqlite3"
    _seed(path)
    reader = ReadOnlyEvidenceRegistry(path)

    with pytest.raises(ReadOnlyEvidenceNotFound):
        reader.get_evidence("evidence-missing")
