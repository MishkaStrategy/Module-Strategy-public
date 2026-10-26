from __future__ import annotations

from pathlib import Path

import pytest

from market_data_ingestion.errors import StorageError
from market_data_ingestion.registry_schema import VersionedSQLiteDatasetRegistry
from market_data_ingestion.storage_recovery import RecoverableLocalStorage
from scripts.benchmark_dataset_storage_slo import _descriptor


def test_unregistered_manifest_is_detected_until_registry_commit(tmp_path: Path) -> None:
    storage = RecoverableLocalStorage(tmp_path / "storage")
    registry = VersionedSQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    descriptor = _descriptor(storage.root, 1)
    storage.write_manifest(
        descriptor.dataset_id,
        descriptor.version,
        {"dataset_id": descriptor.dataset_id, "version": descriptor.version},
    )

    assert storage.find_unregistered_manifests(registry) == (
        f"manifests/{descriptor.dataset_id}/v{descriptor.version}.json",
    )

    registry.register(descriptor)
    assert storage.find_unregistered_manifests(registry) == ()


def test_unregistered_manifest_inventory_rejects_invalid_layout(tmp_path: Path) -> None:
    storage = RecoverableLocalStorage(tmp_path / "storage")
    registry = VersionedSQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    invalid = storage.root / "manifests" / "dataset" / "latest.json"
    invalid.parent.mkdir(parents=True)
    invalid.write_text("{}\n", encoding="utf-8")

    with pytest.raises(StorageError) as captured:
        storage.find_unregistered_manifests(registry)
    assert captured.value.code == "MANIFEST_PATH_INVALID"
