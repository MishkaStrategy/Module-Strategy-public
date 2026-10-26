from __future__ import annotations

from dataclasses import replace
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


def _source(tmp_path: Path) -> StreamingRegisteredCandleSource:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    config = DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path,
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        dataset_version=1,
    )
    return StreamingRegisteredCandleSource(
        config=config,
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        dataset_version=1,
        dataset_hash="a" * 64,
        manifest_checksum="b" * 64,
        schema_fingerprint="c" * 64,
        partition_count=2,
        source_uri="dataset://binance_usdt-m-perpetual_btcusdt_5m/v1",
        row_count=4,
        first_timestamp=start,
        last_timestamp=start + timedelta(minutes=15),
        symbol="BTCUSDT",
        timeframe="5m",
        interval=timedelta(minutes=5),
        bridge_version=BRIDGE_VERSION,
        _selection=SimpleNamespace(
            partitions=(
                SimpleNamespace(start=start, row_count=2),
                SimpleNamespace(start=start + timedelta(minutes=10), row_count=2),
            )
        ),
    )


def test_registered_source_rejects_cursor_timestamp_mismatch(tmp_path: Path) -> None:
    source = _source(tmp_path)
    valid = source._position(
        partition_index=1,
        row_offset=0,
        last_timestamp=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
    )
    tampered = replace(valid, last_timestamp=datetime(2024, 1, 1, tzinfo=UTC))

    with pytest.raises(RegisteredDataError, match="physical row position"):
        source.open(tampered)


def test_registered_source_rejects_inconsistent_finished_cursor(tmp_path: Path) -> None:
    source = _source(tmp_path)
    finished = source._position(
        partition_index=source.partition_count,
        row_offset=0,
        last_timestamp=source.last_timestamp,
    )
    tampered = replace(finished, last_timestamp=source.last_timestamp - timedelta(minutes=5))

    with pytest.raises(RegisteredDataError, match="physical row position"):
        source.open(tampered)


def test_registered_source_rejects_partition_end_alias_cursor(tmp_path: Path) -> None:
    source = _source(tmp_path)
    noncanonical = source._position(
        partition_index=0,
        row_offset=2,
        last_timestamp=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
    )

    with pytest.raises(RegisteredDataError, match="row offset"):
        source.open(noncanonical)
