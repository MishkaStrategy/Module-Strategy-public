from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import market_data_ingestion.streaming_dataset_reader as streaming_reader
from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.enums import MarketType
from market_data_ingestion.errors import StorageError
from market_data_ingestion.external_corpus import ExternalCorpusFile, ExternalCorpusSpec
from market_data_ingestion.external_registration import ExternalCorpusRegistrar
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.streaming_dataset_reader import (
    iter_registered_kline_batches,
    resolve_streaming_registered_kline_dataset,
)
from market_data_ingestion.timeframes import timeframe_delta


def _write_klines(path: Path, open_times: list[datetime]) -> None:
    step = timeframe_delta("5m")
    count = len(open_times)
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [100.0 + index for index in range(count)],
                "high": [101.0 + index for index in range(count)],
                "low": [99.0 + index for index in range(count)],
                "close": [100.5 + index for index in range(count)],
                "volume": [10.0 + index for index in range(count)],
                "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
                "quote_volume": [1000.0 + index for index in range(count)],
                "trades": [10 + index for index in range(count)],
                "taker_buy_base": [5.0 + index for index in range(count)],
                "taker_buy_quote": [500.0 + index for index in range(count)],
            }
        ),
        path,
    )


def _registered(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "BTCUSDT_5m.parquet"
    open_times = [
        datetime(2024, 1, 31, 23, 50, tzinfo=UTC),
        datetime(2024, 1, 31, 23, 55, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 5, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 10, tzinfo=UTC),
    ]
    _write_klines(source, open_times)
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(
            ExternalCorpusFile(
                symbol="BTCUSDT",
                timeframe="5m",
                path=source,
                source_uri="local-corpus://streaming-test/btcusdt/5m",
                expected_rows=len(open_times),
                expected_first_open_time=open_times[0],
                expected_last_open_time=open_times[-1],
                expected_sha256=sha256_file(source),
            ),
        ),
    )
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = ExternalCorpusRegistrar(storage=storage, registry=registry).register(spec)
    return storage.root, result.datasets[0].descriptor.dataset_id


def _selection(tmp_path: Path) -> streaming_reader.StreamingRegisteredKlineSelection:
    storage_root, dataset_id = _registered(tmp_path)
    return resolve_streaming_registered_kline_dataset(storage_root, dataset_id, 1)


def test_streaming_batches_are_bounded_ordered_and_positioned(tmp_path: Path) -> None:
    selection = _selection(tmp_path)

    batches = list(iter_registered_kline_batches(selection, batch_size=1))
    records = [record for batch in batches for record in batch.records]

    assert all(batch.row_count == 1 for batch in batches)
    assert [record.open_time.minute for record in records] == [50, 55, 0, 5, 10]
    assert [(batch.partition_index, batch.row_offset) for batch in batches] == [
        (0, 0),
        (0, 1),
        (1, 0),
        (1, 1),
        (1, 2),
    ]


def test_streaming_reader_resumes_at_partition_row_offset(tmp_path: Path) -> None:
    selection = _selection(tmp_path)

    resumed = list(
        iter_registered_kline_batches(
            selection,
            batch_size=2,
            start_partition_index=1,
            start_row_offset=1,
        )
    )
    records = [record for batch in resumed for record in batch.records]

    assert [record.open_time.minute for record in records] == [5, 10]
    assert resumed[0].partition_index == 1
    assert resumed[0].row_offset == 1


def test_streaming_reader_does_not_use_record_batch_materialization() -> None:
    source = inspect.getsource(streaming_reader)

    assert ".to_pylist(" not in source
    assert ".to_pydict(" not in source
    assert "Table.from_batches" not in source


@pytest.mark.parametrize("batch_size", [0, -1, True, 1_000_001])
def test_streaming_reader_rejects_unbounded_or_invalid_batch_size(
    tmp_path: Path,
    batch_size: int,
) -> None:
    selection = _selection(tmp_path)

    with pytest.raises(ValueError, match="batch_size"):
        list(iter_registered_kline_batches(selection, batch_size=batch_size))


def test_streaming_reader_rejects_partition_changed_after_resolution(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    partition = selection.partitions[0]
    path = selection.storage_root / partition.storage_uri
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(StorageError, match="checksum"):
        list(iter_registered_kline_batches(selection, batch_size=1))


def test_streaming_reader_rejects_missing_partition(tmp_path: Path) -> None:
    selection = _selection(tmp_path)
    partition = selection.partitions[0]
    path = selection.storage_root / partition.storage_uri
    path.unlink()

    with pytest.raises(StorageError, match="missing"):
        list(iter_registered_kline_batches(selection, batch_size=1))


def test_streaming_reader_rejects_schema_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selection = _selection(tmp_path)
    partition = selection.partitions[0]
    path = selection.storage_root / partition.storage_uri
    table = pq.read_table(path).drop(["volume"])
    pq.write_table(table, path)
    real_sha256 = streaming_reader.sha256_file

    def accepted_checksum(candidate: Path) -> str:
        if candidate == path:
            return partition.checksum_sha256
        return real_sha256(candidate)

    monkeypatch.setattr(streaming_reader, "sha256_file", accepted_checksum)
    with pytest.raises(StorageError, match="schema"):
        list(iter_registered_kline_batches(selection, batch_size=1))
