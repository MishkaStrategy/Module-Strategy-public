from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import market_data_ingestion.price_dataset_reader as price_reader
from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.contracts import NormalizedPartitionDescriptor, RawArtifactDescriptor
from market_data_ingestion.enums import DataType, MarketType, SourceKind, ValidationStatus
from market_data_ingestion.errors import RegistryError, StorageError
from market_data_ingestion.price_dataset_reader import resolve_registered_price_dataset

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=5)
MID = START + 2 * STEP
END = START + 4 * STEP


def _write_prices(path: Path, data_type: DataType, opens: list[datetime]) -> None:
    count = len(opens)
    pq.write_table(
        pa.table(
            {
                "data_type": [data_type.value] * count,
                "open_time": opens,
                "close_time": [item + STEP - timedelta(milliseconds=1) for item in opens],
                "open": ["100"] * count,
                "high": ["101"] * count,
                "low": ["99"] * count,
                "close": ["100.5"] * count,
                "symbol": ["BTCUSDT"] * count,
                "timeframe": ["5m"] * count,
                "exchange_id": ["binance"] * count,
                "market_type": ["usdt_m_perpetual"] * count,
                "source_kind": ["official_rest"] * count,
                "is_closed": [True] * count,
            }
        ),
        path,
    )


def _partition(
    path: Path,
    data_type: DataType,
    *,
    partition_id: str,
    start: datetime,
    end: datetime,
    row_count: int = 2,
    source_ids: tuple[str, ...],
    timeframe: str = "5m",
) -> NormalizedPartitionDescriptor:
    return NormalizedPartitionDescriptor(
        partition_id=partition_id,
        data_type=data_type,
        timeframe=timeframe,
        storage_uri=path.name,
        checksum_sha256=sha256_file(path),
        row_count=row_count,
        start=start,
        end=end,
        schema_version=1,
        source_artifact_ids=source_ids,
    )


def _raw(
    artifact_id: str,
    *,
    start: datetime,
    end: datetime,
    record_count: int | None,
) -> RawArtifactDescriptor:
    return RawArtifactDescriptor(
        artifact_id=artifact_id,
        source=SourceKind.OFFICIAL_REST,
        source_uri=f"https://data.binance.example/{artifact_id}",
        retrieved_at=END,
        checksum_sha256=("a" if artifact_id.endswith("a") else "b") * 64,
        byte_size=1024,
        record_count=record_count,
        start=start,
        end=end,
        adapter_version="1.0.0",
    )


def _patch_resolver(
    monkeypatch: pytest.MonkeyPatch,
    root: Path,
    *,
    data_type: DataType,
    descriptor_data_types: tuple[DataType, ...] | None = None,
    descriptor_timeframes: tuple[str, ...] = ("5m",),
    partitions: tuple[NormalizedPartitionDescriptor, ...],
    raw_artifacts: tuple[RawArtifactDescriptor, ...],
    compatible_consumers: tuple[str, ...] = ("modular_backtest",),
    manifest_status: ValidationStatus = ValidationStatus.VALID,
) -> None:
    descriptor = SimpleNamespace(
        dataset_id="price-dataset",
        version=1,
        dataset_hash="c" * 64,
        instrument_id="binance:usdt-m:btcusdt",
        storage_uri=root.as_uri(),
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        compatible_consumers=compatible_consumers,
        data_types=descriptor_data_types or (data_type,),
        timeframes=descriptor_timeframes,
        start=START,
        end=END,
    )
    manifest = SimpleNamespace(
        validation=SimpleNamespace(status=manifest_status),
        partitions=partitions,
        source_artifacts=raw_artifacts,
        instrument=SimpleNamespace(symbol="BTCUSDT"),
        exchange=SimpleNamespace(id="binance"),
        market_type=MarketType.USDT_M_PERPETUAL,
    )

    class FakeRegistry:
        def __init__(self, _path: Path) -> None:
            pass

        def inspect(self, dataset_id: str, version: int) -> dict[str, object] | None:
            assert dataset_id == "price-dataset"
            assert version == 1
            return {"dataset_id": dataset_id, "version": version}

    monkeypatch.setattr(price_reader, "SQLiteDatasetRegistry", FakeRegistry)
    monkeypatch.setattr(price_reader, "descriptor_from_dict", lambda _payload: descriptor)
    monkeypatch.setattr(
        price_reader,
        "verify_registered_dataset",
        lambda *_args, **_kwargs: {"status": "ok"},
    )
    monkeypatch.setattr(price_reader, "load_manifest", lambda *_args, **_kwargs: manifest)


def _two_partition_fixture(
    tmp_path: Path,
    data_type: DataType,
) -> tuple[Path, tuple[NormalizedPartitionDescriptor, ...], tuple[RawArtifactDescriptor, ...]]:
    root = tmp_path / "storage"
    root.mkdir()
    (root / "registry.sqlite3").write_bytes(b"registry")
    first_path = root / "price-a.parquet"
    second_path = root / "price-b.parquet"
    _write_prices(first_path, data_type, [START, START + STEP])
    _write_prices(second_path, data_type, [MID, MID + STEP])
    partitions = (
        _partition(
            first_path,
            data_type,
            partition_id="price-a",
            start=START,
            end=MID,
            source_ids=("raw-a",),
        ),
        _partition(
            second_path,
            data_type,
            partition_id="price-b",
            start=MID,
            end=END,
            source_ids=("raw-b",),
        ),
    )
    raw = (
        _raw("raw-a", start=START, end=MID, record_count=2),
        _raw("raw-b", start=MID, end=END, record_count=2),
    )
    return root, partitions, raw


@pytest.mark.parametrize(
    "data_type",
    [DataType.MARK_PRICE, DataType.INDEX_PRICE, DataType.PREMIUM_INDEX],
)
def test_resolver_accepts_exact_registered_price_owner_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    data_type: DataType,
) -> None:
    root, partitions, raw = _two_partition_fixture(tmp_path, data_type)
    _patch_resolver(
        monkeypatch,
        root,
        data_type=data_type,
        partitions=partitions,
        raw_artifacts=raw,
    )

    first = resolve_registered_price_dataset(
        root,
        "price-dataset",
        1,
        price_type=data_type,
        timeframe="5M",
    )
    second = resolve_registered_price_dataset(
        root,
        "price-dataset",
        1,
        price_type=data_type,
        timeframe="5m",
    )

    assert first.to_payload() == second.to_payload()
    assert first.price_type is data_type
    assert first.timeframe == "5m"
    assert first.row_count == first.source_record_count == 4
    assert first.partition_start_inclusive == START
    assert first.partition_end_exclusive == END
    assert [item.artifact_id for item in first.source_artifacts] == ["raw-a", "raw-b"]
    assert "source_uri" not in str(first.to_payload())


def test_resolver_rejects_wrong_type_timeframe_consumer_and_manifest_status(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, partitions, raw = _two_partition_fixture(tmp_path, DataType.MARK_PRICE)

    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        descriptor_data_types=(DataType.INDEX_PRICE,),
        partitions=partitions,
        raw_artifacts=raw,
    )
    with pytest.raises(RegistryError, match="data type is not registered"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )

    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        descriptor_timeframes=("1m",),
        partitions=partitions,
        raw_artifacts=raw,
    )
    with pytest.raises(RegistryError, match="timeframe is not registered"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )

    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        partitions=partitions,
        raw_artifacts=raw,
        compatible_consumers=("research_runner",),
    )
    with pytest.raises(RegistryError, match="does not permit"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )

    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        partitions=partitions,
        raw_artifacts=raw,
        manifest_status=ValidationStatus.INVALID,
    )
    with pytest.raises(StorageError, match="manifest is not VALID"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )


def test_resolver_rejects_partition_gap_and_source_coverage_gap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, partitions, raw = _two_partition_fixture(tmp_path, DataType.INDEX_PRICE)
    gap_partition = replace(partitions[1], start=MID + STEP)
    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.INDEX_PRICE,
        partitions=(partitions[0], gap_partition),
        raw_artifacts=raw,
    )
    with pytest.raises(StorageError, match="not exactly contiguous"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.INDEX_PRICE,
            timeframe="5m",
        )

    source_gap = (
        _raw("raw-a", start=START, end=MID - STEP, record_count=2),
        _raw("raw-b", start=MID, end=END, record_count=2),
    )
    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.INDEX_PRICE,
        partitions=partitions,
        raw_artifacts=source_gap,
    )
    with pytest.raises(StorageError, match="uncovered acquisition interval"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.INDEX_PRICE,
            timeframe="5m",
        )


def test_resolver_rejects_missing_unknown_and_count_drift_lineage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root, partitions, raw = _two_partition_fixture(tmp_path, DataType.MARK_PRICE)

    missing_count = (
        _raw("raw-a", start=START, end=MID, record_count=None),
        raw[1],
    )
    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        partitions=partitions,
        raw_artifacts=missing_count,
    )
    with pytest.raises(StorageError, match="exact record counts"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )

    drift = (
        _raw("raw-a", start=START, end=MID, record_count=3),
        raw[1],
    )
    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        partitions=partitions,
        raw_artifacts=drift,
    )
    with pytest.raises(StorageError, match="row count differs"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )

    unknown = (
        replace(partitions[0], source_artifact_ids=("unknown-raw",)),
        partitions[1],
    )
    _patch_resolver(
        monkeypatch,
        root,
        data_type=DataType.MARK_PRICE,
        partitions=unknown,
        raw_artifacts=raw,
    )
    with pytest.raises(StorageError, match="unknown reference"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )


def test_resolver_rejects_non_price_type_and_invalid_version(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    with pytest.raises(ValueError, match="price_type"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            1,
            price_type=DataType.KLINES,
            timeframe="5m",
        )
    with pytest.raises(ValueError, match="positive integer"):
        resolve_registered_price_dataset(
            root,
            "price-dataset",
            0,
            price_type=DataType.MARK_PRICE,
            timeframe="5m",
        )
