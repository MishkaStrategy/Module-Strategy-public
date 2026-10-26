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
from modular_backtest.registered_recovery import (
    RegisteredStreamingRecoveryError,
    run_with_registered_streaming_replay_restart,
)
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


class _SingleTradeModule(StrategyModule):
    name = "registered_streaming_recovery_trade"
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
                    client_order_id="registered-recovery-entry",
                ),
            )
        )


def _registered(
    tmp_path: Path,
    *,
    rows: int = 17,
    start_offset: int | None = None,
    end_offset: int | None = None,
) -> tuple[DataSourceConfig, datetime, timedelta]:
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
                source_uri="local-corpus://registered-streaming-recovery/btcusdt/5m",
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
    return (
        DataSourceConfig(
            symbol="BTCUSDT",
            storage_root=storage.root,
            dataset_id=descriptor.dataset_id,
            dataset_version=descriptor.version,
            registered_mode="streaming",
            stream_batch_size=2,
            start=(start + start_offset * step if start_offset is not None else None),
            end=start + end_offset * step if end_offset is not None else None,
        ),
        start,
        step,
    )


def _engine() -> ScalableBacktestEngine:
    return ScalableBacktestEngine(
        _SingleTradeModule(),
        EngineConfig(
            close_open_position_at_end=True,
            data=DataConfig(
                expected_interval_seconds=300,
                gap_policy="reject",
                max_base_history=64,
            ),
        ),
        performance=PerformanceConfig(
            mode="canonical_audit",
            max_memory_mb=None,
            progress_interval_bars=10_000,
            max_base_history=64,
            max_timeframe_history=64,
        ),
    )


def _cleanup(run: object) -> None:
    result = getattr(run, "result", None)
    journal = getattr(result, "journal", None)
    if isinstance(journal, ScalableJournal):
        journal.streaming_store.cleanup()


def test_registered_streaming_replay_reopens_exact_next_row_across_partition(
    tmp_path: Path,
) -> None:
    config, start, step = _registered(tmp_path)
    prepared = prepare_candle_source(config)

    run = run_with_registered_streaming_replay_restart(
        _engine,
        prepared,
        restart_at_index=7,
        seed=17,
        commit_sha="a" * 40,
    )
    try:
        assert run.restart_verified is True
        assert run.continuous_sha256 == run.restarted_sha256
        assert run.continuous_source_open_count == 1
        assert run.restarted_source_open_count == 2
        assert run.recovery_mode == "bounded_deterministic_prefix_replay"
        assert run.simulation_passes == 2
        assert run.source_traversals == 3
        assert run.source_rows_replayed == 8
        assert run.checkpoint.bars_processed == 8
        assert run.checkpoint.kernel.last_bar_index == 7
        assert run.checkpoint.source_position.last_timestamp == start + 7 * step
        assert run.checkpoint.source_position.partition_index == 1
        assert run.checkpoint.source_position.row_offset == 2
        assert run.result.bars_processed == 17
        assert run.result.journal.fills
        assert run.result.journal.trades
        evidence = run.evidence()
        assert evidence["restart_verified"] is True
        assert evidence["recovery_mode"] == "bounded_deterministic_prefix_replay"
        assert evidence["simulation_passes"] == 2
        assert evidence["source_traversals"] == 3
        assert evidence["source_rows_replayed"] == 8
        assert "storage" not in str(evidence["checkpoint"])
    finally:
        _cleanup(run)


@pytest.mark.parametrize("batch_size", [1, 3, 64])
def test_registered_streaming_recovery_is_batch_size_independent(
    tmp_path: Path,
    batch_size: int,
) -> None:
    config, _, _ = _registered(tmp_path)
    prepared = prepare_candle_source(
        config,
        registered_mode="streaming",
        stream_batch_size=batch_size,
    )

    run = run_with_registered_streaming_replay_restart(
        _engine,
        prepared,
        restart_at_index=5,
        seed=23,
    )
    try:
        assert run.restart_verified
        assert run.continuous_sha256 == run.restarted_sha256
        assert run.continuous_source_open_count == 1
        assert run.restarted_source_open_count == 2
        assert run.result.journal.trades
    finally:
        _cleanup(run)


def test_registered_streaming_recovery_respects_selected_range(tmp_path: Path) -> None:
    config, start, step = _registered(
        tmp_path,
        start_offset=3,
        end_offset=14,
    )
    prepared = prepare_candle_source(config)

    run = run_with_registered_streaming_replay_restart(
        _engine,
        prepared,
        restart_at_index=4,
        seed=31,
    )
    try:
        assert prepared.total_bars == 11
        assert run.result.bars_processed == 11
        assert run.checkpoint.bars_processed == 5
        assert run.checkpoint.source_position.last_timestamp == start + 7 * step
        assert run.continuous_sha256 == run.restarted_sha256
    finally:
        _cleanup(run)


def test_registered_streaming_recovery_rejects_legacy_source(tmp_path: Path) -> None:
    config, _, _ = _registered(tmp_path)
    legacy = prepare_candle_source(config, registered_mode="legacy_materialized")

    with pytest.raises(
        RegisteredStreamingRecoveryError,
        match="source_mode=streaming",
    ):
        run_with_registered_streaming_replay_restart(
            _engine,
            legacy,
            restart_at_index=2,
        )


@pytest.mark.parametrize("restart_at_index", [-1, 16, 17, True])
def test_registered_streaming_recovery_rejects_invalid_boundary(
    tmp_path: Path,
    restart_at_index: int,
) -> None:
    config, _, _ = _registered(tmp_path)
    prepared = prepare_candle_source(config)

    with pytest.raises(ValueError, match="leave at least one candle"):
        run_with_registered_streaming_replay_restart(
            _engine,
            prepared,
            restart_at_index=restart_at_index,
        )
