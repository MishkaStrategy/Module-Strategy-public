from __future__ import annotations

import errno
import gc
import json
import os
import sqlite3
import subprocess
import sys
import warnings
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

import pytest

from market_data_ingestion.errors import RegistryError, StorageError
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.registry_schema import (
    REGISTRY_SCHEMA_VERSION,
    VersionedSQLiteDatasetRegistry,
)
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.storage_recovery import RecoverableLocalStorage
from scripts.benchmark_dataset_storage_slo import _descriptor, run_benchmark


def test_manifest_publication_is_not_registry_visibility(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    descriptor = _descriptor(storage.root, 1)
    storage.write_manifest(
        descriptor.dataset_id,
        descriptor.version,
        {"dataset_id": descriptor.dataset_id, "version": descriptor.version},
    )

    assert registry.inspect(descriptor.dataset_id, descriptor.version) is None

    registry.register(descriptor)
    assert registry.inspect(descriptor.dataset_id, descriptor.version) is not None
    reopened = SQLiteDatasetRegistry(registry.path)
    assert reopened.inspect(descriptor.dataset_id, descriptor.version) is not None


def test_manifest_publication_is_idempotent_and_conflicting_content_is_rejected(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    payload = {"dataset_id": "manifest-idempotency", "version": 1}

    first = storage.write_manifest("manifest-idempotency", 1, payload)
    second = storage.write_manifest("manifest-idempotency", 1, payload)

    assert first == second
    with pytest.raises(StorageError) as captured:
        storage.write_manifest(
            "manifest-idempotency",
            1,
            {"dataset_id": "manifest-idempotency", "version": 2},
        )
    assert captured.value.code == "IMMUTABLE_CONFLICT"


def test_failed_atomic_replace_removes_temporary_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")

    def fail_replace(
        source: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        destination: str | bytes | os.PathLike[str] | os.PathLike[bytes],
    ) -> None:
        del source, destination
        raise OSError(errno.EIO, "simulated replace failure")

    monkeypatch.setattr("market_data_ingestion.storage.os.replace", fail_replace)
    with pytest.raises(StorageError) as captured:
        storage.write_manifest("atomic-failure", 1, {"value": 1})
    assert captured.value.code == "STORAGE_IO_ERROR"
    assert not tuple((storage.root / "manifests").rglob(".*.tmp"))


def test_concurrent_registry_readers_observe_only_committed_descriptors(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    descriptors = tuple(_descriptor(storage.root, index) for index in range(1, 51))
    for descriptor in descriptors:
        registry.register(descriptor)

    def read_all() -> int:
        return sum(
            registry.inspect(descriptor.dataset_id, descriptor.version) is not None
            for descriptor in descriptors
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        observed = tuple(executor.map(lambda _: read_all(), range(16)))

    assert observed == (len(descriptors),) * 16


def test_stale_lock_is_reclaimed_but_corrupt_lock_fails_closed(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "storage")
    stale = storage.root / "locks" / "stale.lock"
    stale.write_text(
        "pid=999999999\ncreated_at=2020-01-01T00:00:00+00:00\n",
        encoding="utf-8",
    )
    with storage.lock("stale"):
        assert (storage.root / "locks" / "stale.lock").is_file()
    assert not stale.exists()

    corrupt = storage.root / "locks" / "corrupt.lock"
    corrupt.write_text("not-a-valid-lock\n", encoding="utf-8")
    with pytest.raises(StorageError) as captured, storage.lock("corrupt"):
        pass
    assert captured.value.code == "LOCK_CORRUPT"


def test_versioned_registry_adopts_legacy_schema_idempotently(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "registry.sqlite3"
    legacy = SQLiteDatasetRegistry(path)
    descriptor = _descriptor(root, 1)
    legacy.register(descriptor)

    first = VersionedSQLiteDatasetRegistry(path)
    second = VersionedSQLiteDatasetRegistry(path)

    with closing(sqlite3.connect(path)) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
    assert version == REGISTRY_SCHEMA_VERSION
    assert first.inspect(descriptor.dataset_id, descriptor.version) is not None
    assert second.inspect(descriptor.dataset_id, descriptor.version) is not None


def test_versioned_registry_rejects_future_schema(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(f"PRAGMA user_version={REGISTRY_SCHEMA_VERSION + 1}")

    with pytest.raises(RegistryError) as captured:
        VersionedSQLiteDatasetRegistry(path)
    assert captured.value.code == "REGISTRY_SCHEMA_DOWNGRADE_REJECTED"


def test_versioned_registry_rejects_schema_drift(tmp_path: Path) -> None:
    path = tmp_path / "drift.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute("CREATE TABLE datasets(unexpected TEXT)")
        connection.execute(f"PRAGMA user_version={REGISTRY_SCHEMA_VERSION}")

    with pytest.raises(RegistryError) as captured:
        VersionedSQLiteDatasetRegistry(path)
    assert captured.value.code == "REGISTRY_SCHEMA_INVALID"


def test_versioned_registry_rejects_symlinked_path(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    SQLiteDatasetRegistry(target)
    link = tmp_path / "registry-link.sqlite3"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(RegistryError) as captured:
        VersionedSQLiteDatasetRegistry(link)
    assert captured.value.code == "REGISTRY_PATH_INVALID"


def test_versioned_registry_closes_schema_connections(tmp_path: Path) -> None:
    path = tmp_path / "registry.sqlite3"
    gc.collect()
    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        for _ in range(10):
            VersionedSQLiteDatasetRegistry(path)
        gc.collect()

    resource_warnings = [item for item in captured if issubclass(item.category, ResourceWarning)]
    assert resource_warnings == []


def test_recoverable_storage_lists_manifests_and_cleans_only_aged_orphans(
    tmp_path: Path,
) -> None:
    storage = RecoverableLocalStorage(tmp_path / "storage")
    storage.write_manifest("dataset-b", 1, {"dataset_id": "dataset-b"})
    storage.write_manifest("dataset-a", 1, {"dataset_id": "dataset-a"})
    directory = storage.root / "manifests" / "dataset-a"
    aged = directory / ".v1.json.crash.tmp"
    fresh = directory / ".v1.json.active.tmp"
    unrelated = directory / "notes.tmp"
    aged.write_bytes(b"aged")
    fresh.write_bytes(b"fresh")
    unrelated.write_bytes(b"keep")
    os.utime(aged, (0, 0))

    assert storage.list_manifests() == (
        "manifests/dataset-a/v1.json",
        "manifests/dataset-b/v1.json",
    )
    removed = storage.cleanup_orphan_temporary_files(max_age_seconds=60)

    assert removed == (aged,)
    assert not aged.exists()
    assert fresh.is_file()
    assert unrelated.is_file()


def test_recoverable_storage_manifest_inventory_rejects_symlinks(tmp_path: Path) -> None:
    storage = RecoverableLocalStorage(tmp_path / "storage")
    target = storage.write_manifest("target", 1, {"dataset_id": "target"})
    link = storage.root / "manifests" / "linked.json"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlinks unavailable: {exc}")

    with pytest.raises(StorageError) as captured:
        storage.list_manifests()
    assert captured.value.code == "SYMLINK_PATH"


def test_registry_crash_before_commit_rolls_back_and_after_commit_recovers(
    tmp_path: Path,
) -> None:
    root = tmp_path / "storage"
    registry_path = root / "registry.sqlite3"
    registry = VersionedSQLiteDatasetRegistry(registry_path)

    before_commit = f"""
import os
import sqlite3
from datetime import UTC, datetime
path = {str(registry_path)!r}
connection = sqlite3.connect(path)
connection.execute('PRAGMA journal_mode=WAL')
connection.execute('BEGIN IMMEDIATE')
connection.execute(
    'INSERT INTO datasets VALUES(?,?,?,?,?)',
    ('crash-before', 1, {"ab" * 32!r}, '{{}}', datetime.now(UTC).isoformat()),
)
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", before_commit], check=True)
    assert registry.inspect("crash-before", 1) is None

    after_commit = f"""
import os
from pathlib import Path
from market_data_ingestion.registry_schema import VersionedSQLiteDatasetRegistry
from scripts.benchmark_dataset_storage_slo import _descriptor
root = Path({str(root)!r})
registry = VersionedSQLiteDatasetRegistry(root / 'registry.sqlite3')
registry.register(_descriptor(root, 77))
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", after_commit], check=True)
    reopened = VersionedSQLiteDatasetRegistry(registry_path)
    assert reopened.inspect("bench-dataset-00077", 1) is not None
    with closing(sqlite3.connect(registry_path)) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_dataset_storage_smoke_is_deterministic_and_bounded() -> None:
    result = run_benchmark(
        dataset_counts=(1, 100),
        manifest_count=10,
    )
    reports = Path("reports")
    reports.mkdir(exist_ok=True)
    output = reports / (
        f"dataset-storage-smoke-py{sys.version_info.major}{sys.version_info.minor}.json"
    )
    output.write_text(
        json.dumps(result, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )

    assert [item["dataset_count"] for item in result["registry"]] == [1, 100]
    assert all(
        item["integrity_check_result"] == "ok"
        and item["transaction_rollback_preserved_count"]
        and item["conflicting_registration_classified"]
        for item in result["registry"]
    )
    assert result["local_storage"]["immutable_conflict_detected"] is True
    assert result["local_storage"]["cleanup_api_supported"] is False
    fd_growth = result["process"]["fd_growth"]
    assert fd_growth is None or fd_growth <= 2
