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
from market_data_ingestion.errors import StorageError
from market_data_ingestion.integrity import RegisteredPathError
from market_data_ingestion.price_dataset_reader import (
    RegisteredPriceSelection,
    _price_partition_records,
    iter_registered_prices,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=5)
END = START + 4 * STEP


def _write_prices(
    path: Path,
    data_type: DataType,
    opens: list[datetime],
    *,
    symbol: str = "BTCUSDT",
    timeframe: str = "5m",
    source_kind: str = "official_rest",
    closed: bool = True,
) -> None:
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
                "symbol": [symbol] * count,
                "timeframe": [timeframe] * count,
                "exchange_id": ["binance"] * count,
                "market_type": ["usdt_m_perpetual"] * count,
                "source_kind": [source_kind] * count,
                "is_closed": [closed] * count,
            }
        ),
        path,
    )


def _raw(
    *,
    record_count: int | None = 4,
    artifact_id: str = "raw-price",
    start: datetime = START,
    end: datetime = END,
) -> RawArtifactDescriptor:
    return RawArtifactDescriptor(
        artifact_id=artifact_id,
        source=SourceKind.OFFICIAL_REST,
        source_uri=f"https://data.binance.example/{artifact_id}",
        retrieved_at=END,
        checksum_sha256="a" * 64,
        byte_size=1024,
        record_count=record_count,
        start=start,
        end=end,
        adapter_version="1.0.0",
    )


def _partition(
    path: Path,
    data_type: DataType,
    *,
    row_count: int = 4,
    partition_id: str = "price-2026-01",
    source_ids: tuple[str, ...] = ("raw-price",),
    start: datetime = START,
    end: datetime = END,
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


def _selection(
    root: Path,
    partition: NormalizedPartitionDescriptor,
    *,
    raw: RawArtifactDescriptor | None = None,
) -> RegisteredPriceSelection:
    source = raw or _raw(record_count=partition.row_count, start=partition.start, end=partition.end)
    descriptor = SimpleNamespace(
        dataset_id="binance-btcusdt-price",
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
    return RegisteredPriceSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        price_type=partition.data_type,
        timeframe=partition.timeframe or "5m",
        partitions=(partition,),
        source_artifacts=(source,),
    )


@pytest.mark.parametrize("data_type", [DataType.MARK_PRICE, DataType.INDEX_PRICE])
def test_registered_price_stream_preserves_type_continuity_and_public_evidence(
    tmp_path: Path,
    data_type: DataType,
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "price.parquet"
    opens = [START + index * STEP for index in range(4)]
    _write_prices(path, data_type, opens)
    selection = _selection(root, _partition(path, data_type))

    records = list(iter_registered_prices(selection))
    payload = selection.to_payload()

    assert [item.open_time for item in records] == opens
    assert all(item.data_type is data_type for item in records)
    assert all(item.timeframe == "5m" for item in records)
    assert selection.row_count == selection.source_record_count == 4
    assert payload["data_type"] == data_type.value
    assert payload["timeframe"] == "5m"
    assert payload["dataset_hash"] == "b" * 64
    assert payload["coverage_semantics"] == "[start,end)"
    assert payload["partition_start_inclusive"] == START.isoformat()
    assert payload["partition_end_exclusive"] == END.isoformat()
    assert payload["source_coverage_start_inclusive"] == START.isoformat()
    assert payload["source_coverage_end_exclusive"] == END.isoformat()
    assert "storage_root" not in payload
    artifacts = payload["source_artifacts"]
    assert isinstance(artifacts, list)
    assert artifacts[0]["checksum_sha256"] == "a" * 64
    assert "source_uri" not in artifacts[0]


def test_registered_price_stream_enforces_exact_requested_window(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "price.parquet"
    opens = [START + index * STEP for index in range(4)]
    _write_prices(path, DataType.MARK_PRICE, opens)
    selection = _selection(root, _partition(path, DataType.MARK_PRICE))

    records = list(
        iter_registered_prices(
            selection,
            start=START + STEP,
            end=START + 3 * STEP,
        )
    )

    assert [item.open_time for item in records] == [START + STEP, START + 2 * STEP]

    with pytest.raises(StorageError, match="start at the requested boundary"):
        list(iter_registered_prices(selection, start=START + timedelta(minutes=1), end=END))
    with pytest.raises(StorageError, match="after registered partition coverage"):
        list(iter_registered_prices(selection, end=END + STEP))


def test_price_partition_rejects_checksum_schema_and_row_count_drift(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "price.parquet"
    opens = [START + index * STEP for index in range(4)]
    _write_prices(path, DataType.INDEX_PRICE, opens)
    partition = _partition(path, DataType.INDEX_PRICE)
    selection = _selection(root, partition)

    with pytest.raises(StorageError, match="checksum"):
        list(_price_partition_records(selection, replace(partition, checksum_sha256="0" * 64)))

    with pytest.raises(StorageError, match="row count"):
        list(_price_partition_records(selection, replace(partition, row_count=5)))

    schema_path = root / "wrong-schema.parquet"
    pq.write_table(pa.table({"unexpected": [1]}), schema_path)
    schema_partition = replace(
        partition,
        storage_uri=schema_path.name,
        checksum_sha256=sha256_file(schema_path),
    )
    with pytest.raises(StorageError, match="canonical schema"):
        list(_price_partition_records(selection, schema_partition))


def test_price_partition_rejects_wrong_type_identity_and_bounds(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "price.parquet"
    opens = [START + index * STEP for index in range(4)]
    _write_prices(path, DataType.INDEX_PRICE, opens)
    partition = _partition(path, DataType.INDEX_PRICE)
    selection = _selection(root, partition)

    wrong_type = root / "wrong-type.parquet"
    _write_prices(wrong_type, DataType.MARK_PRICE, opens)
    with pytest.raises(ValueError, match="data_type"):
        list(
            _price_partition_records(
                selection,
                replace(
                    partition,
                    storage_uri=wrong_type.name,
                    checksum_sha256=sha256_file(wrong_type),
                ),
            )
        )

    wrong_symbol = root / "wrong-symbol.parquet"
    _write_prices(wrong_symbol, DataType.INDEX_PRICE, opens, symbol="ETHUSDT")
    with pytest.raises(StorageError, match="identity"):
        list(
            _price_partition_records(
                selection,
                replace(
                    partition,
                    storage_uri=wrong_symbol.name,
                    checksum_sha256=sha256_file(wrong_symbol),
                ),
            )
        )

    late_path = root / "late.parquet"
    late_opens = [START + STEP, START + 2 * STEP]
    _write_prices(late_path, DataType.INDEX_PRICE, late_opens)
    narrow = replace(
        partition,
        storage_uri=late_path.name,
        checksum_sha256=sha256_file(late_path),
        row_count=2,
        start=START + 2 * STEP,
    )
    with pytest.raises(StorageError, match="outside partition bounds"):
        list(_price_partition_records(selection, narrow))


def test_price_partition_rejects_unsafe_path_and_source_kind_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "storage"
    root.mkdir()
    path = root / "price.parquet"
    opens = [START + index * STEP for index in range(4)]
    _write_prices(path, DataType.MARK_PRICE, opens)
    partition = _partition(path, DataType.MARK_PRICE)
    selection = _selection(root, partition)

    with pytest.raises(ValueError, match="safe relative path"):
        replace(partition, storage_uri="../escape.parquet")

    with monkeypatch.context() as scoped:

        def reject_registered_path(*_args: object, **_kwargs: object) -> Path:
            raise RegisteredPathError("non-traded price path escapes storage root")

        scoped.setattr(price_reader, "resolve_registered_path", reject_registered_path)
        with pytest.raises(StorageError, match="path is unsafe"):
            list(_price_partition_records(selection, partition))

    other_source = root / "other-source.parquet"
    _write_prices(other_source, DataType.MARK_PRICE, opens, source_kind="official_archive")
    with pytest.raises(StorageError, match="identity"):
        list(
            _price_partition_records(
                selection,
                replace(
                    partition,
                    storage_uri=other_source.name,
                    checksum_sha256=sha256_file(other_source),
                ),
            )
        )


def test_registered_price_stream_rejects_duplicate_and_gap(tmp_path: Path) -> None:
    root = tmp_path / "storage"
    root.mkdir()

    duplicate_path = root / "duplicate.parquet"
    duplicate = [START, START]
    _write_prices(duplicate_path, DataType.MARK_PRICE, duplicate)
    duplicate_partition = _partition(
        duplicate_path,
        DataType.MARK_PRICE,
        row_count=2,
        end=START + 2 * STEP,
    )
    duplicate_selection = _selection(root, duplicate_partition)
    with pytest.raises(StorageError, match="duplicate open timestamp"):
        list(iter_registered_prices(duplicate_selection))

    gap_path = root / "gap.parquet"
    gap = [START, START + 2 * STEP]
    _write_prices(gap_path, DataType.MARK_PRICE, gap)
    gap_partition = _partition(
        gap_path,
        DataType.MARK_PRICE,
        row_count=2,
        end=START + 3 * STEP,
    )
    gap_selection = _selection(root, gap_partition)
    with pytest.raises(StorageError, match="not exactly contiguous"):
        list(iter_registered_prices(gap_selection))
