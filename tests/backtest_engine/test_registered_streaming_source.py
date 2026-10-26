from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

pytest.importorskip("market_data_ingestion")

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.enums import MarketType
from market_data_ingestion.external_corpus import ExternalCorpusFile, ExternalCorpusSpec
from market_data_ingestion.external_registration import ExternalCorpusRegistrar
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.timeframes import timeframe_delta
from modular_backtest.config import DataSourceConfig
from modular_backtest.registered_data import (
    BRIDGE_VERSION,
    RegisteredDataError,
    open_registered_candles,
    open_streaming_registered_candles,
)


def _registered(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "BTCUSDT_5m.parquet"
    step = timeframe_delta("5m")
    open_times = [
        datetime(2024, 1, 31, 23, 50, tzinfo=UTC),
        datetime(2024, 1, 31, 23, 55, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 5, tzinfo=UTC),
        datetime(2024, 2, 1, 0, 10, tzinfo=UTC),
    ]
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
        source,
    )
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(
            ExternalCorpusFile(
                symbol="BTCUSDT",
                timeframe="5m",
                path=source,
                source_uri="local-corpus://streaming-source-test/btcusdt/5m",
                expected_rows=count,
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


def _config(
    storage_root: Path,
    dataset_id: str,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> DataSourceConfig:
    return DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=1,
        start=start,
        end=end,
    )


def test_registered_source_is_replayable_and_reports_manifest_metadata(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=1,
    )

    first = tuple(source.candles)
    second = tuple(source.candles)
    metadata = source.metadata().to_dict()

    assert first == second
    assert source.source_open_count == 2
    assert source.row_count == 5
    assert metadata["dataset_id"] == dataset_id
    assert metadata["bridge_version"] == BRIDGE_VERSION
    assert metadata["partition_count"] == 2
    assert len(str(metadata["manifest_checksum"])) == 64
    assert len(str(metadata["schema_fingerprint"])) == 64
    assert str(storage_root) not in str(metadata)


def test_registered_streaming_candles_match_legacy_sequence(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    config = _config(storage_root, dataset_id)

    legacy = tuple(open_registered_candles(config).candles)
    streaming = tuple(open_streaming_registered_candles(config, batch_size=1).open())

    assert streaming == legacy


def test_registered_source_defers_partition_hashing_until_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_data_ingestion.streaming_dataset_reader as streaming_reader

    storage_root, dataset_id = _registered(tmp_path)
    real_sha256 = streaming_reader.sha256_file
    hashed: list[Path] = []

    def tracked(path: Path) -> str:
        hashed.append(path)
        return real_sha256(path)

    monkeypatch.setattr(streaming_reader, "sha256_file", tracked)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=1,
    )

    assert hashed
    assert all(path.suffix != ".parquet" for path in hashed)
    next(source.open())
    assert any(path.suffix == ".parquet" for path in hashed)


def test_registered_source_checkpoint_resumes_at_exact_next_row(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=1,
    )
    baseline = tuple(source.open())

    iterator = source.open()
    first = next(iterator)
    second = next(iterator)
    position = iterator.checkpoint_position()
    iterator.close()
    resumed = tuple(source.open(position))

    assert (first, second, *resumed) == baseline
    assert position.last_timestamp == second.timestamp
    assert position.partition_index == 1
    assert position.row_offset == 0
    assert position.manifest_checksum == source.manifest_checksum
    assert position.schema_fingerprint == source.schema_fingerprint
    assert position.source_version == BRIDGE_VERSION


def test_registered_source_finished_cursor_reopens_as_empty_iterator(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=2,
    )
    iterator = source.open()
    tuple(iterator)
    finished = iterator.checkpoint_position()

    assert finished.partition_index == source.partition_count
    assert tuple(source.open(finished)) == ()


def test_registered_source_range_end_cursor_reopens_as_empty_iterator(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(
            storage_root,
            dataset_id,
            end=datetime(2024, 2, 1, 0, 0, tzinfo=UTC),
        ),
        batch_size=1,
    )
    iterator = source.open()
    candles = tuple(iterator)
    finished_range = iterator.checkpoint_position()

    assert [candle.timestamp.minute for candle in candles] == [50, 55]
    assert finished_range.partition_index == 1
    assert finished_range.row_offset == 0
    assert finished_range.last_timestamp == candles[-1].timestamp
    assert tuple(source.open(finished_range)) == ()


def test_registered_source_rejects_cursor_from_other_dataset(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(_config(storage_root, dataset_id))
    foreign = replace(source.initial_position(), dataset_id="different-dataset")

    with pytest.raises(RegisteredDataError, match="different dataset identity"):
        source.open(foreign)


def test_registered_source_start_range_opens_at_first_relevant_row(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(
            storage_root,
            dataset_id,
            start=datetime(2024, 2, 1, 0, 5, tzinfo=UTC),
        ),
        batch_size=1,
    )

    position = source.initial_position()
    candles = tuple(source.open())

    assert position.partition_index == 1
    assert position.row_offset == 1
    assert [candle.timestamp.minute for candle in candles] == [5, 10]


def test_registered_source_result_is_batch_size_independent(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    config = _config(storage_root, dataset_id)

    one = tuple(open_streaming_registered_candles(config, batch_size=1).open())
    two = tuple(open_streaming_registered_candles(config, batch_size=2).open())
    large = tuple(open_streaming_registered_candles(config, batch_size=100).open())

    assert one == two == large


def test_registered_source_propagates_failure_after_partial_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_data_ingestion.streaming_dataset_reader as streaming_reader

    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=1,
    )
    real_iter = streaming_reader.iter_registered_kline_batches

    def fail_after_first(*args: object, **kwargs: object) -> Iterator[object]:
        batches = real_iter(*args, **kwargs)
        yield next(batches)
        raise OSError("synthetic partition failure")

    monkeypatch.setattr(streaming_reader, "iter_registered_kline_batches", fail_after_first)
    iterator = source.open()

    first = next(iterator)
    position = iterator.checkpoint_position()
    with pytest.raises(RegisteredDataError, match="synthetic partition failure"):
        next(iterator)

    assert first.timestamp == datetime(2024, 1, 31, 23, 50, tzinfo=UTC)
    assert position.row_offset == 1
    assert position.last_timestamp == first.timestamp


def test_registered_source_closes_partition_reader_on_cancellation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_data_ingestion.streaming_dataset_reader as streaming_reader

    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=1,
    )
    real_iter = streaming_reader.iter_registered_kline_batches
    closed: list[bool] = []

    def tracked(*args: object, **kwargs: object) -> Iterator[object]:
        batches = real_iter(*args, **kwargs)
        try:
            yield from batches
        finally:
            closed.append(True)

    monkeypatch.setattr(streaming_reader, "iter_registered_kline_batches", tracked)
    iterator = source.open()

    next(iterator)
    iterator.close()

    assert closed == [True]


def test_registered_source_detects_duplicate_across_batch_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_data_ingestion.streaming_dataset_reader as streaming_reader

    storage_root, dataset_id = _registered(tmp_path)
    source = open_streaming_registered_candles(
        _config(storage_root, dataset_id),
        batch_size=1,
    )
    real_iter = streaming_reader.iter_registered_kline_batches

    def duplicate_first(*args: object, **kwargs: object) -> Iterator[object]:
        batches = real_iter(*args, **kwargs)
        first = next(batches)
        yield first
        yield first

    monkeypatch.setattr(streaming_reader, "iter_registered_kline_batches", duplicate_first)
    iterator = source.open()

    next(iterator)
    with pytest.raises(RegisteredDataError, match="duplicate timestamp"):
        next(iterator)
