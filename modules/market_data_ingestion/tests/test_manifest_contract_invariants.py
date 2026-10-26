from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import DatasetManifest, MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.manifest_io import load_manifest
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def _manifest(tmp_path: Path) -> DatasetManifest:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    request = MarketDataRequest(
        request_id="manifest-contract",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=start,
        end=start + timedelta(hours=1),
    )
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=storage,
        registry=registry,
    ).execute(request, format_name="csv")
    if result.dataset is None:
        raise AssertionError("fixture ingestion did not register a dataset")
    return load_manifest(storage.root, result.dataset)


def test_manifest_rejects_duplicate_partition_ids(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    duplicate = manifest.partitions[0]

    with pytest.raises(ValueError, match="partition IDs must be unique"):
        replace(manifest, partitions=manifest.partitions + (duplicate,))


def test_manifest_rejects_duplicate_raw_artifact_ids(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    duplicate = manifest.source_artifacts[0]

    with pytest.raises(ValueError, match="raw artifact IDs must be unique"):
        replace(manifest, source_artifacts=manifest.source_artifacts + (duplicate,))


def test_manifest_rejects_unknown_partition_lineage(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    changed = replace(
        manifest.partitions[0],
        source_artifact_ids=("raw:missing",),
    )

    with pytest.raises(ValueError, match="unknown references"):
        replace(manifest, partitions=(changed, *manifest.partitions[1:]))


def test_manifest_rejects_partition_self_reference(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    partition = manifest.partitions[0]
    changed = replace(
        partition,
        source_artifact_ids=(partition.partition_id,),
    )

    with pytest.raises(ValueError, match="cannot reference itself"):
        replace(manifest, partitions=(changed, *manifest.partitions[1:]))


def test_manifest_rejects_partition_lineage_cycle(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    first, second, *rest = manifest.partitions
    first = replace(first, source_artifact_ids=(second.partition_id,))
    second = replace(second, source_artifact_ids=(first.partition_id,))

    with pytest.raises(ValueError, match="contains a cycle"):
        replace(manifest, partitions=(first, second, *rest))


def test_manifest_rejects_declared_data_type_mismatch(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    with pytest.raises(ValueError, match="data_types must exactly match"):
        replace(
            manifest,
            data_types=manifest.data_types + (DataType.FUNDING_RATE,),
        )


def test_manifest_rejects_declared_timeframe_mismatch(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)

    with pytest.raises(ValueError, match="timeframes must exactly match"):
        replace(manifest, timeframes=manifest.timeframes + ("1h",))


@pytest.mark.parametrize(
    "storage_uri",
    (
        "../normalized/data.csv",
        "/absolute/data.csv",
        "normalized\\data.csv",
        "normalized/../../data.csv",
    ),
)
def test_partition_rejects_unsafe_relative_path(
    tmp_path: Path,
    storage_uri: str,
) -> None:
    manifest = _manifest(tmp_path)

    with pytest.raises((TypeError, ValueError)):
        replace(manifest.partitions[0], storage_uri=storage_uri)


def test_interval_partition_requires_timeframe(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    interval = next(item for item in manifest.partitions if item.data_type is DataType.KLINES)

    with pytest.raises(ValueError, match="requires timeframe"):
        replace(interval, timeframe=None)


def test_event_partition_rejects_timeframe(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    metadata = next(
        item for item in manifest.partitions if item.data_type is DataType.INSTRUMENT_METADATA
    )

    with pytest.raises(ValueError, match="must not declare timeframe"):
        replace(metadata, timeframe="5m")
