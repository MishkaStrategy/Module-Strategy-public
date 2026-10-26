from __future__ import annotations

import builtins
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_backtest import data as data_module
from modular_backtest.config import DataSourceConfig
from modular_backtest.data import (
    DataValidationError,
    _number,
    _parse_timestamp,
    _validated_stream,
    file_sha256,
    load_validated_candles,
    stream_candles,
    validate_data,
)
from modular_core.models import Candle


def _write(path: Path, rows: str, header: str = "open_time,open,high,low,close,volume\n") -> None:
    path.write_text(header + rows, encoding="utf-8")


def _config(path: Path, **kwargs: object) -> DataSourceConfig:
    values: dict[str, object] = {"path": path, "format": "csv", "symbol": "BTCUSDT"}
    values.update(kwargs)
    return DataSourceConfig(**values)  # type: ignore[arg-type]


def test_csv_validation_and_utc_epoch_ms(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(path, "1704067200000,100,110,90,105,10\n1704067500000,105,112,100,111,12\n")
    candles, summary = load_validated_candles(_config(path))
    assert len(candles) == 2
    assert candles[0].timestamp == datetime(2024, 1, 1, tzinfo=UTC)
    assert summary.rows == 2
    assert summary.gaps == 0
    assert summary.sha256 == file_sha256(path)
    assert validate_data(_config(path)).to_dict()["rows"] == 2


def test_iso_timestamp_and_custom_columns(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(
        path,
        "2024-01-01T00:00:00+00:00,100,110,90,105,10\n",
        "ts,o,h,l,c,v\n",
    )
    config = _config(
        path,
        timestamp_column="ts",
        columns={"open": "o", "high": "h", "low": "l", "close": "c", "volume": "v"},
    )
    assert list(stream_candles(config))[0].close == 105


def test_gap_allow_is_reported(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(path, "1704067200000,100,110,90,105,10\n1704067800000,105,112,100,111,12\n")
    _, summary = load_validated_candles(_config(path, gap_policy="allow"))
    assert summary.gaps == 1


def test_gap_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(path, "1704067200000,100,110,90,105,10\n1704067800000,105,112,100,111,12\n")
    with pytest.raises(DataValidationError, match="5m gap"):
        load_validated_candles(_config(path))


def test_duplicate_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(path, "1704067200000,100,110,90,105,10\n1704067200000,105,112,100,111,12\n")
    with pytest.raises(DataValidationError, match="duplicate timestamp"):
        load_validated_candles(_config(path))


def test_out_of_order_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(path, "1704067500000,100,110,90,105,10\n1704067200000,105,112,100,111,12\n")
    with pytest.raises(DataValidationError, match="strictly increasing"):
        load_validated_candles(_config(path))


def test_validated_stream_derives_gap_interval_from_timeframe() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    candles = [
        Candle(start, 100.0, 101.0, 99.0, 100.5, 10.0, "BTCUSDT", "1h"),
        Candle(
            start + timedelta(hours=1),
            100.5,
            102.0,
            100.0,
            101.0,
            11.0,
            "BTCUSDT",
            "1h",
        ),
    ]

    validated, gaps, duplicates = _validated_stream(candles, "reject", timeframe="1h")

    assert validated == candles
    assert gaps == 0
    assert duplicates == 0


@pytest.mark.parametrize(
    ("rows", "message"),
    [
        ("", "empty"),
        ("1704067200000,nope,110,90,105,10\n", "not numeric"),
        ("1704067200000,nan,110,90,105,10\n", "finite"),
        ("2024-01-01T00:00:00,100,110,90,105,10\n", "timezone-aware"),
    ],
)
def test_invalid_rows_are_rejected(tmp_path: Path, rows: str, message: str) -> None:
    path = tmp_path / "bars.csv"
    _write(path, rows)
    with pytest.raises(DataValidationError, match=message):
        load_validated_candles(_config(path))


def test_boolean_and_fractional_epoch_values_are_rejected() -> None:
    with pytest.raises(DataValidationError, match="boolean"):
        _parse_timestamp(True)
    with pytest.raises(DataValidationError, match="integer epoch"):
        _parse_timestamp(1704067200.5)
    with pytest.raises(DataValidationError, match="not numeric"):
        _number(False, "volume")


def test_source_mutation_during_validation_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bars.csv"
    _write(path, "1704067200000,100,110,90,105,10\n")
    original_sha = file_sha256(path)
    hashes = iter((original_sha, "0" * 64))
    monkeypatch.setattr(data_module, "file_sha256", lambda source: next(hashes))

    with pytest.raises(DataValidationError, match="changed during validation"):
        load_validated_candles(_config(path))


def test_missing_columns_and_file_are_rejected(tmp_path: Path) -> None:
    missing = tmp_path / "missing.csv"
    with pytest.raises(DataValidationError, match="does not exist"):
        list(stream_candles(_config(missing)))
    path = tmp_path / "bars.csv"
    _write(path, "1704067200000,100,110,90,105\n", "open_time,open,high,low,close\n")
    with pytest.raises(DataValidationError, match="missing columns"):
        load_validated_candles(_config(path))


def test_parquet_requires_optional_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "bars.parquet"
    path.write_bytes(b"placeholder")
    config = DataSourceConfig(path=path, format="parquet", symbol="BTCUSDT")
    real_import = builtins.__import__

    def deny_pyarrow(name: str, *args: object, **kwargs: object) -> object:
        if name.startswith("pyarrow"):
            raise ImportError("not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", deny_pyarrow)
    with pytest.raises(DataValidationError, match="optional dependency"):
        next(stream_candles(config))


def test_date_range_is_start_inclusive_end_exclusive(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write(
        path,
        "1704067200000,100,110,90,105,10\n"
        "1704067500000,105,112,100,111,12\n"
        "1704067800000,111,115,108,114,9\n",
    )
    config = _config(
        path,
        start=datetime(2024, 1, 1, 0, 5, tzinfo=UTC),
        end=datetime(2024, 1, 1, 0, 10, tzinfo=UTC),
    )
    candles, summary = load_validated_candles(config)
    assert [candle.timestamp.minute for candle in candles] == [5]
    assert summary.requested_start == "2024-01-01T00:05:00+00:00"
    assert summary.requested_end == "2024-01-01T00:10:00+00:00"
