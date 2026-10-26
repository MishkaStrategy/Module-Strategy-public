from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from modular_backtest.data import DataValidationError, load_validated_candles
from modular_backtest.registered_data import open_registered_candles


def _registered(tmp_path: Path) -> tuple[Path, str]:
    source = tmp_path / "BTCUSDT_5m.parquet"
    step = timeframe_delta("5m")
    open_times = [
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 10, tzinfo=UTC),
    ]
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [100.0, 101.0, 102.0],
                "high": [101.0, 102.0, 103.0],
                "low": [99.0, 100.0, 101.0],
                "close": [100.5, 101.5, 102.5],
                "volume": [10.0, 11.0, 12.0],
                "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
                "quote_volume": [1000.0, 1100.0, 1200.0],
                "trades": [10, 11, 12],
                "taker_buy_base": [5.0, 5.5, 6.0],
                "taker_buy_quote": [500.0, 550.0, 600.0],
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
                source_uri="local-corpus://btcusdt/5m",
                expected_rows=3,
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


def test_backtest_loads_exact_registered_dataset_without_local_path_leak(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    config = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=1,
        start=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
        end=datetime(2024, 1, 1, 0, 15, tzinfo=UTC),
    )

    candles, summary = load_validated_candles(config)

    assert [item.timestamp.minute for item in candles] == [5, 10]
    assert summary.path == f"dataset://{dataset_id}/v1"
    assert summary.dataset_id == dataset_id
    assert summary.dataset_version == 1
    assert summary.dataset_hash == summary.sha256
    assert summary.partition_count == 1
    assert str(storage_root) not in str(summary.to_dict())


def test_registered_candle_source_defers_record_iteration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_data_ingestion.dataset_reader as dataset_reader

    consumed: list[str] = []
    selection = SimpleNamespace(
        manifest=SimpleNamespace(instrument=SimpleNamespace(symbol="BTCUSDT")),
        descriptor=SimpleNamespace(
            dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
            version=1,
            dataset_hash="a" * 64,
        ),
        partitions=(object(),),
    )

    def fake_resolve(*_args: object, **_kwargs: object) -> SimpleNamespace:
        return selection

    def fake_iter(*_args: object, **_kwargs: object) -> Iterator[SimpleNamespace]:
        consumed.append("started")
        yield SimpleNamespace(
            open_time=datetime(2024, 1, 1, tzinfo=UTC),
            open="100.0",
            high="101.0",
            low="99.0",
            close="100.5",
            volume="10.0",
            symbol="BTCUSDT",
            timeframe="5m",
        )

    monkeypatch.setattr(dataset_reader, "resolve_registered_kline_dataset", fake_resolve)
    monkeypatch.setattr(dataset_reader, "iter_registered_klines", fake_iter)
    config = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path,
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        dataset_version=1,
    )

    source = open_registered_candles(config)

    assert consumed == []
    candles = tuple(source.candles)
    assert consumed == ["started"]
    assert len(candles) == 1
    assert candles[0].close == 100.5


def test_backtest_rejects_registered_dataset_symbol_mismatch(tmp_path: Path) -> None:
    storage_root, dataset_id = _registered(tmp_path)
    config = DataSourceConfig(
        symbol="ETHUSDT",
        storage_root=storage_root,
        dataset_id=dataset_id,
        dataset_version=1,
    )

    with pytest.raises(DataValidationError, match="symbol differs"):
        load_validated_candles(config)
