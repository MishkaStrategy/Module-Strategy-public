from __future__ import annotations

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
from modular_backtest.data import DataValidationError
from modular_backtest.scalability import (
    PerformanceConfig,
    ScalableBacktestEngine,
    ScalableJournal,
)
from modular_backtest.streaming_data import prepare_candle_source
from modular_core.config import DataConfig, EngineConfig
from modular_core.manifest import ModuleManifest
from modular_core.models import OrderRequest, OrderType, Side
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule

_MODE_ENV = "MODULAR_BACKTEST_REGISTERED_DATASET_MODE"
_BATCH_ENV = "MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE"


class _NoopModule(StrategyModule):
    name = "registered_streaming_integration_noop"
    manifest = ModuleManifest(name=name, version="0.1.0", api_version="1.0")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


class _SingleTradeModule(StrategyModule):
    name = "registered_streaming_integration_trade"
    manifest = ModuleManifest(
        name=name,
        version="0.1.0",
        api_version="1.0",
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index != 0:
            return ModuleDecision()
        return ModuleDecision(
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    quantity=1.0,
                    stop_loss=95.0,
                    take_profit=101.0,
                    client_order_id="registered-stream-equivalence-entry",
                ),
            )
        )


def _registered(tmp_path: Path, *, rows: int = 17) -> DataSourceConfig:
    source = tmp_path / "BTCUSDT_5m.parquet"
    step = timeframe_delta("5m")
    start = datetime(2024, 1, 31, 23, 30, tzinfo=UTC)
    open_times = [start + index * step for index in range(rows)]
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [100.0 for _ in range(rows)],
                "high": [101.0 for _ in range(rows)],
                "low": [99.0 for _ in range(rows)],
                "close": [100.0 for _ in range(rows)],
                "volume": [10.0 + index for index in range(rows)],
                "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
                "quote_volume": [1000.0 + index for index in range(rows)],
                "trades": [10 + index for index in range(rows)],
                "taker_buy_base": [5.0 + index for index in range(rows)],
                "taker_buy_quote": [500.0 + index for index in range(rows)],
            }
        ),
        source,
        compression="zstd",
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
                source_uri="local-corpus://registered-streaming-integration/btcusdt/5m",
                expected_rows=rows,
                expected_first_open_time=open_times[0],
                expected_last_open_time=open_times[-1],
                expected_sha256=sha256_file(source),
            ),
        ),
    )
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    registered = ExternalCorpusRegistrar(storage=storage, registry=registry).register(spec)
    descriptor = registered.datasets[0].descriptor
    return DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=storage.root,
        dataset_id=descriptor.dataset_id,
        dataset_version=descriptor.version,
    )


def _engine(module: StrategyModule | None = None) -> ScalableBacktestEngine:
    selected_module = module or _NoopModule()
    return ScalableBacktestEngine(
        selected_module,
        EngineConfig(
            close_open_position_at_end=True,
            data=DataConfig(
                expected_interval_seconds=300,
                gap_policy="reject",
                max_base_history=64,
            ),
        ),
        performance=PerformanceConfig(
            mode="research_fast",
            max_memory_mb=None,
            progress_interval_bars=10_000,
            max_base_history=64,
            max_timeframe_history=64,
        ),
    )


def _cleanup(result: object) -> None:
    journal = getattr(result, "journal", None)
    if isinstance(journal, ScalableJournal):
        journal.streaming_store.cleanup()


def test_registered_prepared_source_defaults_to_legacy_mode(tmp_path: Path) -> None:
    prepared = prepare_candle_source(_registered(tmp_path))
    metadata = prepared.validation.to_dict()

    assert prepared.source_mode == "legacy_materialized"
    assert prepared.stream_batch_size is None
    assert metadata["source_mode"] == "legacy_materialized"
    assert metadata["stream_batch_size"] is None
    assert len(tuple(prepared.iter_candles())) == 17


def test_registered_prepared_streaming_matches_legacy_sequence_and_hash(
    tmp_path: Path,
) -> None:
    config = _registered(tmp_path)
    legacy = prepare_candle_source(config)
    streaming = prepare_candle_source(
        config,
        registered_mode="streaming",
        stream_batch_size=2,
    )
    metadata = streaming.validation.to_dict()

    assert streaming.source_mode == "streaming"
    assert streaming.stream_batch_size == 2
    assert metadata["source_mode"] == "streaming"
    assert metadata["stream_batch_size"] == 2
    assert tuple(streaming.iter_candles()) == tuple(legacy.iter_candles())
    assert streaming.validation.dataset_id == legacy.validation.dataset_id
    assert streaming.validation.dataset_version == legacy.validation.dataset_version
    assert streaming.validation.dataset_hash == legacy.validation.dataset_hash
    assert streaming.validation.partition_count == legacy.validation.partition_count
    assert streaming.validation.rows == legacy.validation.rows
    assert streaming.validation.first_timestamp == legacy.validation.first_timestamp
    assert streaming.validation.last_timestamp == legacy.validation.last_timestamp
    assert streaming.data_hash == legacy.data_hash
    assert streaming.first_candle == legacy.first_candle
    assert streaming.last_candle == legacy.last_candle


def test_registered_streaming_mode_is_frozen_between_validation_and_simulation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import modular_backtest.streaming_data as streaming_data

    config = _registered(tmp_path)
    monkeypatch.setenv(_MODE_ENV, "streaming")
    monkeypatch.setenv(_BATCH_ENV, "1")
    prepared = prepare_candle_source(config)

    monkeypatch.setenv(_MODE_ENV, "legacy_materialized")

    def unexpected_legacy_open(_config: DataSourceConfig) -> object:
        raise AssertionError("prepared source changed back to the legacy loader")

    monkeypatch.setattr(streaming_data, "open_registered_candles", unexpected_legacy_open)

    assert prepared.source_mode == "streaming"
    assert prepared.stream_batch_size == 1
    assert len(tuple(prepared.iter_candles())) == 17


def test_registered_streaming_result_is_batch_size_independent(tmp_path: Path) -> None:
    config = _registered(tmp_path)
    one = prepare_candle_source(
        config,
        registered_mode="streaming",
        stream_batch_size=1,
    )
    seven = prepare_candle_source(
        config,
        registered_mode="streaming",
        stream_batch_size=7,
    )

    result_one = _engine().run_prepared(one)
    result_seven = _engine().run_prepared(seven)
    try:
        assert result_one.run_manifest.run_id == result_seven.run_manifest.run_id
        assert result_one.run_manifest.data_hash == result_seven.run_manifest.data_hash
        assert result_one.bars_processed == result_seven.bars_processed == 17
        assert result_one.metrics == result_seven.metrics
        assert list(result_one.journal.fills) == list(result_seven.journal.fills)
        assert list(result_one.journal.trades) == list(result_seven.journal.trades)
        assert list(result_one.journal.equity) == list(result_seven.journal.equity)
    finally:
        _cleanup(result_one)
        _cleanup(result_seven)


def test_registered_streaming_and_legacy_have_golden_trading_equivalence(
    tmp_path: Path,
) -> None:
    config = _registered(tmp_path)
    legacy = prepare_candle_source(config)
    streaming = prepare_candle_source(
        config,
        registered_mode="streaming",
        stream_batch_size=3,
    )

    legacy_result = _engine(_SingleTradeModule()).run_prepared(legacy)
    streaming_result = _engine(_SingleTradeModule()).run_prepared(streaming)
    try:
        assert legacy_result.journal.orders
        assert legacy_result.journal.fills
        assert legacy_result.journal.trades
        assert legacy_result.run_manifest.run_id == streaming_result.run_manifest.run_id
        assert legacy_result.run_manifest.data_hash == streaming_result.run_manifest.data_hash
        assert legacy_result.bars_processed == streaming_result.bars_processed == 17
        assert legacy_result.metrics == streaming_result.metrics
        assert list(legacy_result.journal.fills) == list(streaming_result.journal.fills)
        assert list(legacy_result.journal.trades) == list(streaming_result.journal.trades)
        assert list(legacy_result.journal.equity) == list(streaming_result.journal.equity)
        assert [
            (event.timestamp, event.event, event.order, event.reason)
            for event in legacy_result.journal.orders
        ] == [
            (event.timestamp, event.event, event.order, event.reason)
            for event in streaming_result.journal.orders
        ]
        assert list(legacy_result.journal.errors) == list(streaming_result.journal.errors)
    finally:
        _cleanup(legacy_result)
        _cleanup(streaming_result)


@pytest.mark.parametrize(
    ("mode", "batch", "message"),
    [
        ("unknown", None, "mode must be one of"),
        ("streaming", 0, "batch size must be a positive integer"),
        ("streaming", -1, "batch size must be a positive integer"),
        ("legacy_materialized", 1, "batch size requires"),
    ],
)
def test_registered_loading_options_fail_closed(
    tmp_path: Path,
    mode: str,
    batch: int | None,
    message: str,
) -> None:
    config = _registered(tmp_path)

    with pytest.raises(DataValidationError, match=message):
        prepare_candle_source(
            config,
            registered_mode=mode,
            stream_batch_size=batch,
        )


def test_registered_streaming_environment_rejects_invalid_batch_size(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(_MODE_ENV, "streaming")
    monkeypatch.setenv(_BATCH_ENV, "not-an-integer")

    with pytest.raises(DataValidationError, match="batch size must be a positive integer"):
        prepare_candle_source(_registered(tmp_path))
