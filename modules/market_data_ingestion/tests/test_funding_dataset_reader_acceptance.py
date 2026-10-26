from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.contracts import NormalizedPartitionDescriptor, RawArtifactDescriptor
from market_data_ingestion.enums import DataType, MarketType, SourceKind, ValidationStatus
from market_data_ingestion.errors import StorageError
from market_data_ingestion.funding_dataset_reader import (
    RegisteredFundingSelection,
    _resolve_raw_lineage,
    iter_registered_funding,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)


def _write_funding(path: Path, timestamps: list[datetime]) -> None:
    pq.write_table(
        pa.table(
            {
                "funding_time": timestamps,
                "funding_rate": ["0.0001"] * len(timestamps),
                "mark_price": ["90000"] * len(timestamps),
                "symbol": ["BTCUSDT"] * len(timestamps),
                "exchange_id": ["binance"] * len(timestamps),
                "market_type": ["usdt_m_perpetual"] * len(timestamps),
                "source_kind": ["official_rest"] * len(timestamps),
            }
        ),
        path,
    )


def _raw(*, artifact_id: str = "raw-funding", record_count: int = 2) -> RawArtifactDescriptor:
    return RawArtifactDescriptor(
        artifact_id=artifact_id,
        source=SourceKind.OFFICIAL_REST,
        source_uri=f"https://data.binance.example/{artifact_id}",
        retrieved_at=END,
        checksum_sha256="a" * 64,
        byte_size=1024,
        record_count=record_count,
        start=START,
        end=END,
        adapter_version="1.0.0",
    )


def _partition(
    path: Path,
    *,
    source_ids: tuple[str, ...] = ("raw-funding",),
) -> NormalizedPartitionDescriptor:
    return NormalizedPartitionDescriptor(
        partition_id="funding-2026-01",
        data_type=DataType.FUNDING_RATE,
        timeframe=None,
        storage_uri=path.name,
        checksum_sha256=sha256_file(path),
        row_count=2,
        start=START,
        end=END,
        schema_version=1,
        source_artifact_ids=source_ids,
    )


def _selection(
    root: Path,
    partition: NormalizedPartitionDescriptor,
) -> RegisteredFundingSelection:
    descriptor = SimpleNamespace(
        dataset_id="binance-btcusdt-funding",
        version=1,
        dataset_hash="b" * 64,
        instrument_id="binance:usdt-m:btcusdt",
        validation_status=ValidationStatus.VALID,
    )
    manifest = SimpleNamespace(
        instrument=SimpleNamespace(symbol="BTCUSDT"),
        exchange=SimpleNamespace(id="binance"),
        market_type=MarketType.USDT_M_PERPETUAL,
    )
    return RegisteredFundingSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        partitions=(partition,),
        source_artifacts=(_raw(),),
    )


def test_registered_funding_stream_rejects_out_of_order_timestamp(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "funding.parquet"
    _write_funding(path, [START + timedelta(hours=8), START])
    selection = _selection(root, _partition(path))

    with pytest.raises(StorageError, match="strictly ordered"):
        list(iter_registered_funding(selection))


def test_funding_partition_descriptor_rejects_unsafe_storage_path(tmp_path: Path) -> None:
    path = tmp_path / "funding.parquet"
    _write_funding(path, [START, START + timedelta(hours=8)])

    with pytest.raises(ValueError, match="storage"):
        NormalizedPartitionDescriptor(
            partition_id="funding-unsafe",
            data_type=DataType.FUNDING_RATE,
            timeframe=None,
            storage_uri="../outside.parquet",
            checksum_sha256=sha256_file(path),
            row_count=2,
            start=START,
            end=END,
            schema_version=1,
            source_artifact_ids=("raw-funding",),
        )


def test_registered_funding_lineage_rejects_unknown_reference(tmp_path: Path) -> None:
    path = tmp_path / "funding.parquet"
    _write_funding(path, [START, START + timedelta(hours=8)])
    partition = _partition(path, source_ids=("missing-raw",))
    manifest = SimpleNamespace(
        partitions=(partition,),
        source_artifacts=(_raw(),),
    )

    with pytest.raises(StorageError, match="unknown reference"):
        _resolve_raw_lineage(manifest, (partition,), require_record_counts=True)
