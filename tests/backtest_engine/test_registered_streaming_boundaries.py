from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("market_data_ingestion")

from modular_backtest.config import DataSourceConfig
from modular_backtest.registered_data import (
    BRIDGE_VERSION,
    RegisteredDataError,
    StreamingRegisteredCandleSource,
)


def _source(
    tmp_path: Path,
    *,
    gap_policy: str = "reject",
    end: datetime | None = None,
) -> StreamingRegisteredCandleSource:
    config = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path,
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        dataset_version=1,
        gap_policy=gap_policy,
        end=end,
    )
    start = datetime(2024, 1, 1, tzinfo=UTC)
    selection = SimpleNamespace(
        partitions=(SimpleNamespace(start=start, row_count=2),),
    )
    return StreamingRegisteredCandleSource(
        config=config,
        dataset_id=config.dataset_id,
        dataset_version=config.dataset_version,
        dataset_hash="a" * 64,
        manifest_checksum="b" * 64,
        schema_fingerprint="c" * 64,
        partition_count=1,
        source_uri=f"dataset://{config.dataset_id}/v1",
        row_count=2,
        first_timestamp=start,
        last_timestamp=start + timedelta(minutes=10),
        symbol=config.symbol,
        timeframe=config.base_timeframe,
        interval=timedelta(minutes=5),
        batch_size=1,
        bridge_version=BRIDGE_VERSION,
        _selection=selection,
    )


def _record(timestamp: datetime) -> SimpleNamespace:
    return SimpleNamespace(
        open_time=timestamp,
        open="100.0",
        high="101.0",
        low="99.0",
        close="100.5",
        volume="10.0",
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _gapped_batches(*_args: object, **_kwargs: object) -> Iterator[SimpleNamespace]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    yield SimpleNamespace(
        partition_index=0,
        row_offset=0,
        records=(_record(start),),
    )
    yield SimpleNamespace(
        partition_index=0,
        row_offset=1,
        records=(_record(start + timedelta(minutes=10)),),
    )


@pytest.mark.parametrize("gap_policy", ["reject", "allow"])
def test_registered_source_detects_gap_across_batch_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gap_policy: str,
) -> None:
    import market_data_ingestion.streaming_dataset_reader as streaming_reader

    monkeypatch.setattr(
        streaming_reader,
        "iter_registered_kline_batches",
        _gapped_batches,
    )
    iterator = _source(tmp_path, gap_policy=gap_policy).open()

    next(iterator)
    with pytest.raises(RegisteredDataError, match="5m gap"):
        next(iterator)


def test_registered_source_does_not_validate_gap_beyond_selected_end(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import market_data_ingestion.streaming_dataset_reader as streaming_reader

    start = datetime(2024, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(
        streaming_reader,
        "iter_registered_kline_batches",
        _gapped_batches,
    )

    candles = tuple(_source(tmp_path, end=start + timedelta(minutes=5)).open())

    assert [candle.timestamp for candle in candles] == [start]
