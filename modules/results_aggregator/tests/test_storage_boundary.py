from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from conftest import make_package
from modular_results_registry import RegistrationPackage, ResourceLimitError, SQLiteRegistry


def _package(index: int) -> RegistrationPackage:
    package = make_package(
        package_id=f"storage-boundary-package-{index:03d}",
        experiment_id=f"storage-boundary-experiment-{index:03d}",
        configuration_value=index,
        run_id=f"storage-boundary-run-{index:03d}",
        artifact_id=f"storage-boundary-artifact-{index:03d}",
        metric_value=float(index),
    )
    evidence = replace(
        package.evidence[0],
        evidence_id=f"storage-boundary-evidence-{index:03d}",
    )
    return replace(package, evidence=(evidence,))


def _durable_counts(connection: sqlite3.Connection) -> dict[str, int]:
    queries = {
        "packages": "SELECT COUNT(*) FROM packages",
        "artifacts": "SELECT COUNT(*) FROM artifacts",
        "experiments": "SELECT COUNT(*) FROM experiments",
        "runs": "SELECT COUNT(*) FROM runs",
        "audit_log": "SELECT COUNT(*) FROM audit_log",
    }
    counts: dict[str, int] = {}
    for name, query in queries.items():
        row = connection.execute(query).fetchone()
        assert row is not None
        counts[name] = int(row[0])
    return counts


def _package_count(connection: sqlite3.Connection, package_id: str) -> int:
    row = connection.execute(
        "SELECT COUNT(*) FROM packages WHERE package_id=?",
        (package_id,),
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_registry_sqlite_full_is_sanitized_atomic_and_retryable(tmp_path: Path) -> None:
    path = tmp_path / "results-registry.sqlite3"
    with SQLiteRegistry(path) as registry:
        original_max = registry._connection.execute("PRAGMA max_page_count").fetchone()
        page_count = registry._connection.execute("PRAGMA page_count").fetchone()
        assert original_max is not None and page_count is not None
        registry._connection.execute("PRAGMA max_page_count = " + str(int(page_count[0])))

        failed_package: RegistrationPackage | None = None
        revision_before_failure = registry.revision()
        counts_before_failure = _durable_counts(registry._connection)
        for index in range(1, 65):
            package = _package(index)
            revision_before_attempt = registry.revision()
            counts_before_attempt = _durable_counts(registry._connection)
            try:
                outcome = registry.register_package(package)
            except ResourceLimitError as exc:
                assert "database or disk is full" not in str(exc).lower()
                failed_package = package
                revision_before_failure = revision_before_attempt
                counts_before_failure = counts_before_attempt
                break
            except sqlite3.Error as exc:
                pytest.fail(f"raw SQLite error escaped Results registry: {type(exc).__name__}")
            else:
                assert outcome.state == "REGISTERED"

        assert failed_package is not None, "SQLite boundary was not reached within 64 registrations"
        integrity = registry._connection.execute("PRAGMA integrity_check").fetchone()
        assert integrity is not None and str(integrity[0]) == "ok"
        assert registry.revision() == revision_before_failure
        assert _durable_counts(registry._connection) == counts_before_failure
        assert _package_count(registry._connection, failed_package.package_id) == 0

        registry._connection.execute("PRAGMA max_page_count = " + str(int(original_max[0])))
        registered = registry.register_package(failed_package)
        assert registered.state == "REGISTERED"
        revision_after_registration = registry.revision()
        duplicate = registry.register_package(failed_package)
        assert duplicate.state == "DUPLICATE"
        assert registry.revision() == revision_after_registration

    with SQLiteRegistry(path) as reopened:
        integrity = reopened._connection.execute("PRAGMA integrity_check").fetchone()
        assert integrity is not None and str(integrity[0]) == "ok"
        assert _package_count(reopened._connection, failed_package.package_id) == 1
        replay = reopened.register_package(failed_package)
        assert replay.state == "DUPLICATE"
