from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import market_data_ingestion.dataset_reader as dataset_reader
from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.dataset_reader import (
    _partition_records,
    iter_registered_klines,
    resolve_registered_kline_dataset,
)
from market_data_ingestion.enums import MarketType
from market_data_ingestion.errors import RegistryError, StorageError
from market_data_ingestion.external_corpus import ExternalCorpusFile, ExternalCorpusSpec
from market_data_ingestion.external_registration import ExternalCorpusRegistrar
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
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
        datetime(2024, 1, 31, 23, 55, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 5, tzinfo=UTC),
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
                source_uri="local-corpus://btcusdt/5m",
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


def test_registered_kline_reader_resolves_exact_version_and_filters_utc_range(
    tmp_path: Path,
) -> None:
    storage_root, dataset_id = _registered(tmp_path)

    selection = resolve_registered_kline_dataset(storage_root, dataset_id, 1)
    records = list(
        iter_registered_klines(
            selection,
            start=datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
            end=datetime(2024, 2, 1, 0, 5, tzinfo=UTC),
        )
    )

    assert selection.to_payload()["validation_status"] == "VALID"
    assert selection.row_count == 3
    assert len(selection.partitions) == 2
    assert [record.open_time.minute for record in records] == [0]
    assert records[0].symbol == "BTCUSDT"
    assert records[0].timeframe == "5m"


def test_partition_reader_defers_post_read_checksum_until_exhaustion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    selection = resolve_registered_kline_dataset(storage_root, dataset_id, 1)
    partition = selection.partitions[0]
    checksum_calls: list[Path] = []

    def tracked_sha256(path: Path) -> str:
        checksum_calls.append(path)
        return partition.checksum_sha256

    monkeypatch.setattr(dataset_reader, "sha256_file", tracked_sha256)
    records = _partition_records(selection, partition)

    first = next(records)

    assert first.open_time == partition.start
    assert len(checksum_calls) == 1
    with pytest.raises(StopIteration):
        next(records)
    assert len(checksum_calls) == 2


def test_partition_reader_rejects_row_count_mismatch(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    selection = resolve_registered_kline_dataset(storage_root, dataset_id, 1)
    partition = replace(
        selection.partitions[0],
        row_count=selection.partitions[0].row_count + 1,
    )

    with pytest.raises(StorageError, match="row count"):
        list(_partition_records(selection, partition))


def test_partition_reader_rejects_change_during_stream(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    selection = resolve_registered_kline_dataset(storage_root, dataset_id, 1)
    partition = selection.partitions[0]
    hashes = iter((partition.checksum_sha256, "0" * 64))
    monkeypatch.setattr(dataset_reader, "sha256_file", lambda _path: next(hashes))

    with pytest.raises(StorageError, match="changed while"):
        list(_partition_records(selection, partition))


def test_registered_kline_reader_rejects_missing_version(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)

    with pytest.raises(RegistryError, match="not registered"):
        resolve_registered_kline_dataset(storage_root, dataset_id, 2)


def test_registered_kline_reader_rejects_tampered_partition(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    selection = resolve_registered_kline_dataset(storage_root, dataset_id, 1)
    partition = selection.partitions[0]
    path = storage_root / partition.storage_uri
    path.write_bytes(path.read_bytes() + b"tampered")

    with pytest.raises(StorageError, match="integrity"):
        resolve_registered_kline_dataset(storage_root, dataset_id, 1)


def test_registered_kline_reader_rejects_symlink_storage_root(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    link = tmp_path / "storage-link"
    try:
        link.symlink_to(storage_root, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - platform capability
        pytest.skip(f"symlink creation unavailable: {exc}")

    with pytest.raises(StorageError, match="symbolic link"):
        resolve_registered_kline_dataset(link, dataset_id, 1)


def test_registered_kline_reader_requires_utc_bounds(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    selection = resolve_registered_kline_dataset(storage_root, dataset_id, 1)

    with pytest.raises(ValueError, match="timezone-aware"):
        list(iter_registered_klines(selection, start=datetime(2024, 2, 1)))
