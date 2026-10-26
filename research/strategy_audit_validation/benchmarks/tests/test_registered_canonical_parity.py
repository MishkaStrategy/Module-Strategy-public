from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pandas as pd
from modular_backtest.config import ConfigurationError
from modular_backtest.registered_data import RegisteredDataError

from benchmarks.registered_canonical_parity import (
    _candle_record,
    _expect_fail_closed_error,
    _frame_from_records,
    _registered_config,
)


def test_registered_candle_record_preserves_exact_ohlcv_and_close_time() -> None:
    candle = SimpleNamespace(
        timestamp=datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.5,
        volume=12.0,
    )

    record = _candle_record(candle)
    frame = _frame_from_records([record])

    assert list(frame.columns) == [
        "open_time",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "close_time",
    ]
    assert frame.loc[0, "open_time"] == pd.Timestamp("2024-01-01T00:00:00Z")
    assert frame.loc[0, "close_time"] == pd.Timestamp("2024-01-01T00:04:59.999Z")
    assert str(frame["open_time"].dtype) == "datetime64[ms, UTC]"
    assert str(frame["close_time"].dtype) == "datetime64[ms, UTC]"
    assert frame.loc[0, ["open", "high", "low", "close", "volume"]].tolist() == [
        100.0,
        101.0,
        99.0,
        100.5,
        12.0,
    ]
    float_columns = ("open", "high", "low", "close", "volume")
    assert all(str(frame[column].dtype) == "float64" for column in float_columns)


def test_registered_config_uses_streaming_identity_and_exclusive_end(tmp_path) -> None:
    config = _registered_config(
        storage_root=tmp_path,
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        dataset_version=1,
        start=pd.Timestamp("2024-01-01T00:00:00Z"),
        end=pd.Timestamp("2024-01-02T00:00:00Z"),
        batch_size=4096,
    )

    assert config.registered_mode == "streaming"
    assert config.stream_batch_size == 4096
    assert config.dataset_version == 1
    assert config.start == datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    assert config.end == datetime(2024, 1, 2, 0, 0, tzinfo=UTC)


def test_negative_contract_helper_accepts_source_boundary_rejection() -> None:
    def rejected() -> None:
        raise RegisteredDataError("rejected")

    assert _expect_fail_closed_error("wrong_identity", rejected) == "wrong_identity"


def test_negative_contract_helper_accepts_configuration_boundary_rejection() -> None:
    def rejected() -> None:
        raise ConfigurationError("rejected")

    assert _expect_fail_closed_error("wrong_timeframe", rejected) == "wrong_timeframe"
