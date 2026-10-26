from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.cli import main
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.errors import StorageError
from market_data_ingestion.exporting import export_registered_dataset
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def _registered(tmp_path: Path) -> tuple[LocalStorage, SQLiteDatasetRegistry, object]:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = MarketDataRequest(
        request_id="cli-base",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    ).execute(request, format_name="csv")
    assert result.dataset is not None
    return storage, registry, result.dataset


def test_export_copies_verified_registered_dataset(tmp_path: Path) -> None:
    storage, registry, descriptor = _registered(tmp_path)
    payload = registry.inspect(descriptor.dataset_id, descriptor.version)
    assert payload is not None
    target = tmp_path / "exported"
    report = export_registered_dataset(
        storage_root=storage.root,
        descriptor_payload=payload,
        target=target,
    )
    assert report["status"] == "ok"
    assert (target / "descriptor.json").is_file()
    assert (target / "manifest.json").is_file()
    export_manifest = json.loads((target / "export-manifest.json").read_text())
    assert export_manifest["dataset_hash"] == descriptor.dataset_hash
    assert any(item.get("partition_id") for item in export_manifest["files"])


def test_export_refuses_corrupted_source(tmp_path: Path) -> None:
    storage, registry, descriptor = _registered(tmp_path)
    payload = registry.inspect(descriptor.dataset_id, descriptor.version)
    assert payload is not None
    manifest = json.loads((storage.root / descriptor.manifest_uri).read_text())
    partition = storage.root / manifest["partitions"][0]["storage_uri"]
    partition.write_bytes(partition.read_bytes() + b"tamper")
    with pytest.raises(StorageError, match="integrity verification"):
        export_registered_dataset(
            storage_root=storage.root,
            descriptor_payload=payload,
            target=tmp_path / "bad-export",
        )


def test_cli_update_backfill_and_export_are_executable(tmp_path: Path, capsys) -> None:
    storage, registry, descriptor = _registered(tmp_path)
    assert (
        main(
            [
                "update",
                "--storage-root",
                str(storage.root),
                "--dataset-id",
                descriptor.dataset_id,
                "--to",
                "2026-01-01T02:00:00Z",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    capsys.readouterr()
    latest = registry.inspect(descriptor.dataset_id)
    assert latest is not None and latest["version"] == 2

    assert (
        main(
            [
                "backfill",
                "--storage-root",
                str(storage.root),
                "--dataset-id",
                descriptor.dataset_id,
                "--data-type",
                "funding_rate",
                "--format",
                "csv",
            ]
        )
        == 0
    )
    capsys.readouterr()
    latest = registry.inspect(descriptor.dataset_id)
    assert latest is not None and latest["version"] == 3

    target = tmp_path / "cli-export"
    assert (
        main(
            [
                "export",
                "--storage-root",
                str(storage.root),
                "--dataset-id",
                descriptor.dataset_id,
                "--target",
                str(target),
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert (target / "export-manifest.json").is_file()


def test_export_refuses_partition_symlink_even_when_target_bytes_match(tmp_path: Path) -> None:
    storage, registry, descriptor = _registered(tmp_path)
    payload = registry.inspect(descriptor.dataset_id, descriptor.version)
    assert payload is not None
    manifest = json.loads((storage.root / descriptor.manifest_uri).read_text())
    partition = storage.root / manifest["partitions"][0]["storage_uri"]
    backup = tmp_path / "partition-backup"
    backup.write_bytes(partition.read_bytes())
    partition.unlink()
    try:
        partition.symlink_to(backup)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")
    with pytest.raises(StorageError, match="integrity verification"):
        export_registered_dataset(
            storage_root=storage.root,
            descriptor_payload=payload,
            target=tmp_path / "symlink-export",
        )


def test_export_refuses_manifest_symlink(tmp_path: Path) -> None:
    storage, registry, descriptor = _registered(tmp_path)
    payload = registry.inspect(descriptor.dataset_id, descriptor.version)
    assert payload is not None
    manifest = storage.root / descriptor.manifest_uri
    backup = tmp_path / "manifest-backup.json"
    backup.write_bytes(manifest.read_bytes())
    manifest.unlink()
    try:
        manifest.symlink_to(backup)
    except OSError:
        pytest.skip("symbolic links are unavailable in this environment")
    with pytest.raises(StorageError, match="integrity verification"):
        export_registered_dataset(
            storage_root=storage.root,
            descriptor_payload=payload,
            target=tmp_path / "manifest-symlink-export",
        )
