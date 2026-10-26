from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modular_backtest import streaming_data
from modular_backtest.config import ConfigurationError, DataSourceConfig, load_config
from modular_backtest.streaming_data import prepare_candle_source
from modular_core.models import Candle

_MODE_ENV = "MODULAR_BACKTEST_REGISTERED_DATASET_MODE"
_BATCH_ENV = "MODULAR_BACKTEST_REGISTERED_STREAM_BATCH_SIZE"


def _registered_config(
    tmp_path: Path,
    *,
    mode: str = "legacy_materialized",
    batch_size: int | None = None,
) -> DataSourceConfig:
    return DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=tmp_path / "storage",
        dataset_id="btc-streaming-config",
        dataset_version=1,
        registered_mode=mode,
        stream_batch_size=batch_size,
    )


def _candles() -> tuple[Candle, ...]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return tuple(
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=10.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(3)
    )


def _source(*, mode: str, batch_size: int | None = None) -> SimpleNamespace:
    values = _candles()
    common: dict[str, Any] = {
        "source_uri": "dataset://btc-streaming-config/v1",
        "dataset_id": "btc-streaming-config",
        "dataset_version": 1,
        "dataset_hash": "a" * 64,
        "partition_count": 1,
        "candles": iter(values),
    }
    if mode == "streaming":
        common["batch_size"] = batch_size
    return SimpleNamespace(**common)


def test_registered_config_defaults_to_legacy_mode(tmp_path: Path) -> None:
    config = _registered_config(tmp_path)

    assert config.registered_mode == "legacy_materialized"
    assert config.stream_batch_size is None


def test_registered_streaming_config_defaults_batch_size(tmp_path: Path) -> None:
    config = _registered_config(tmp_path, mode="STREAMING")

    assert config.registered_mode == "streaming"
    assert config.stream_batch_size == 8_192


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"registered_mode": "unknown"}, "registered_mode"),
        (
            {"registered_mode": "legacy_materialized", "stream_batch_size": 1},
            "requires data.registered_mode = streaming",
        ),
        ({"registered_mode": "streaming", "stream_batch_size": 0}, "positive integer"),
        ({"registered_mode": "streaming", "stream_batch_size": True}, "positive integer"),
    ],
)
def test_registered_loader_config_rejects_invalid_values(
    tmp_path: Path,
    kwargs: dict[str, object],
    message: str,
) -> None:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "storage_root": tmp_path / "storage",
        "dataset_id": "btc-streaming-config",
        "dataset_version": 1,
    }
    values.update(kwargs)

    with pytest.raises(ConfigurationError, match=message):
        DataSourceConfig(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"registered_mode": "streaming"},
        {"stream_batch_size": 8},
    ],
)
def test_file_source_rejects_registered_loader_options(
    tmp_path: Path,
    kwargs: dict[str, object],
) -> None:
    values: dict[str, object] = {
        "symbol": "BTCUSDT",
        "path": tmp_path / "bars.csv",
        "format": "csv",
    }
    values.update(kwargs)

    with pytest.raises(ConfigurationError, match="registered dataset source"):
        DataSourceConfig(**values)  # type: ignore[arg-type]


def test_yaml_loads_registered_streaming_mode_and_batch(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        json.dumps(
            {
                "data": {
                    "symbol": "BTCUSDT",
                    "storage_root": "registered-data",
                    "dataset_id": "btc-streaming-config",
                    "dataset_version": 7,
                    "registered_mode": "streaming",
                    "stream_batch_size": 64,
                },
                "modules": "modules.yaml",
            }
        ),
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.data.storage_root == (tmp_path / "registered-data").resolve()
    assert config.data.dataset_id == "btc-streaming-config"
    assert config.data.dataset_version == 7
    assert config.data.registered_mode == "streaming"
    assert config.data.stream_batch_size == 64


def test_prepare_uses_yaml_config_when_api_and_environment_are_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _registered_config(tmp_path, mode="streaming", batch_size=23)
    observed: list[int] = []

    def open_streaming(_config: DataSourceConfig, *, batch_size: int) -> SimpleNamespace:
        observed.append(batch_size)
        return _source(mode="streaming", batch_size=batch_size)

    monkeypatch.setattr(streaming_data, "open_streaming_registered_candles", open_streaming)
    monkeypatch.setattr(
        streaming_data,
        "open_registered_candles",
        lambda _config: pytest.fail("legacy loader was selected"),
    )

    prepared = prepare_candle_source(config)

    assert prepared.source_mode == "streaming"
    assert prepared.stream_batch_size == 23
    assert tuple(prepared.iter_candles()) == _candles()
    assert observed == [23, 23]


def test_environment_overrides_yaml_loader_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _registered_config(tmp_path, mode="streaming", batch_size=23)
    monkeypatch.setenv(_MODE_ENV, "legacy_materialized")
    monkeypatch.delenv(_BATCH_ENV, raising=False)
    observed = 0

    def open_legacy(_config: DataSourceConfig) -> SimpleNamespace:
        nonlocal observed
        observed += 1
        return _source(mode="legacy_materialized")

    monkeypatch.setattr(streaming_data, "open_registered_candles", open_legacy)
    monkeypatch.setattr(
        streaming_data,
        "open_streaming_registered_candles",
        lambda _config, batch_size: pytest.fail(f"streaming loader was selected with {batch_size}"),
    )

    prepared = prepare_candle_source(config)

    assert prepared.source_mode == "legacy_materialized"
    assert prepared.stream_batch_size is None
    assert tuple(prepared.iter_candles()) == _candles()
    assert observed == 2


def test_api_overrides_environment_and_yaml_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config = _registered_config(tmp_path)
    monkeypatch.setenv(_MODE_ENV, "legacy_materialized")
    monkeypatch.delenv(_BATCH_ENV, raising=False)
    observed: list[int] = []

    def open_streaming(_config: DataSourceConfig, *, batch_size: int) -> SimpleNamespace:
        observed.append(batch_size)
        return _source(mode="streaming", batch_size=batch_size)

    monkeypatch.setattr(streaming_data, "open_streaming_registered_candles", open_streaming)
    monkeypatch.setattr(
        streaming_data,
        "open_registered_candles",
        lambda _config: pytest.fail("legacy loader was selected"),
    )

    prepared = prepare_candle_source(
        config,
        registered_mode="streaming",
        stream_batch_size=31,
    )

    assert prepared.source_mode == "streaming"
    assert prepared.stream_batch_size == 31
    assert tuple(prepared.iter_candles()) == _candles()
    assert observed == [31, 31]
