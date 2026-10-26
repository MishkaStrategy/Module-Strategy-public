from __future__ import annotations

import csv
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from modular_backtest.config import DataSourceConfig
from modular_backtest.data import DataValidationError, load_validated_candles
from modular_backtest.scalability.manifest import incremental_candle_data_hash
from modular_backtest.streaming_data import prepare_candle_source


def _write_csv(
    path: Path,
    *,
    count: int = 5,
    duplicate_at: int | None = None,
    gap_at: int | None = None,
) -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("open_time", "open", "high", "low", "close", "volume"),
        )
        writer.writeheader()
        for index in range(count):
            timestamp_index = index
            if duplicate_at is not None and index == duplicate_at:
                timestamp_index -= 1
            if gap_at is not None and index >= gap_at:
                timestamp_index += 1
            price = 100.0 + index
            writer.writerow(
                {
                    "open_time": (start + timedelta(minutes=5 * timestamp_index)).isoformat(),
                    "open": price,
                    "high": price + 1.0,
                    "low": price - 1.0,
                    "close": price + 0.5,
                    "volume": 10.0 + index,
                }
            )


def _config(path: Path, *, gap_policy: str = "reject") -> DataSourceConfig:
    return DataSourceConfig(
        symbol="BTCUSDT",
        path=path,
        format="csv",
        gap_policy=gap_policy,
    )


def test_prepared_source_matches_legacy_validation_and_hash(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write_csv(path)
    config = _config(path)

    legacy, legacy_summary = load_validated_candles(config)
    prepared = prepare_candle_source(config)
    first_pass = list(prepared.iter_candles())
    second_pass = list(prepared.iter_candles())

    assert first_pass == legacy
    assert second_pass == legacy
    assert prepared.validation == legacy_summary
    assert prepared.data_hash == incremental_candle_data_hash(legacy)
    assert prepared.total_bars == len(legacy)
    assert prepared.first_candle == legacy[0]
    assert prepared.last_candle == legacy[-1]


def test_prepared_source_rejects_file_mutation_between_passes(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write_csv(path)
    prepared = prepare_candle_source(_config(path))
    _write_csv(path, count=6)

    with pytest.raises(DataValidationError, match="identity changed"):
        list(prepared.iter_candles())


def test_streaming_validation_rejects_duplicates(tmp_path: Path) -> None:
    path = tmp_path / "duplicate.csv"
    _write_csv(path, duplicate_at=3)

    with pytest.raises(DataValidationError, match="duplicate timestamp"):
        prepare_candle_source(_config(path))


def test_streaming_validation_rejects_gaps_when_configured(tmp_path: Path) -> None:
    path = tmp_path / "gap.csv"
    _write_csv(path, gap_at=3)

    with pytest.raises(DataValidationError, match="5m gap"):
        prepare_candle_source(_config(path))


def test_streaming_validation_counts_allowed_gaps(tmp_path: Path) -> None:
    path = tmp_path / "gap.csv"
    _write_csv(path, gap_at=3)

    prepared = prepare_candle_source(_config(path, gap_policy="allow"))

    assert prepared.validation.gaps == 1
    assert len(list(prepared.iter_candles())) == 5


def test_prepared_source_resume_matches_exact_suffix(tmp_path: Path) -> None:
    path = tmp_path / "bars.csv"
    _write_csv(path, count=8)
    prepared = prepare_candle_source(_config(path))
    complete = list(prepared.iter_candles())

    assert list(prepared.iter_candles_from(3)) == complete[3:]


def test_parquet_resume_does_not_construct_skipped_candles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    path = tmp_path / "bars.parquet"
    start = datetime(2024, 1, 1, tzinfo=UTC)
    rows = 10
    table = pa.table(
        {
            "open_time": [start + timedelta(minutes=5 * index) for index in range(rows)],
            "open": [100.0 + index for index in range(rows)],
            "high": [101.0 + index for index in range(rows)],
            "low": [99.0 + index for index in range(rows)],
            "close": [100.5 + index for index in range(rows)],
            "volume": [10.0 + index for index in range(rows)],
        }
    )
    pq.write_table(table, path, row_group_size=3)
    config = DataSourceConfig(symbol="BTCUSDT", path=path, format="parquet")
    prepared = prepare_candle_source(config)

    import modular_backtest.data as data_module

    original = data_module._row_to_candle
    constructed = 0

    def counted(row: Mapping[str, object], source: DataSourceConfig):
        nonlocal constructed
        constructed += 1
        return original(row, source)

    monkeypatch.setattr(data_module, "_row_to_candle", counted)
    suffix = list(prepared.iter_candles_from(4))

    assert len(suffix) == 6
    assert suffix[0].timestamp == start + timedelta(minutes=20)
    assert constructed == 6


def test_prepared_source_resume_preserves_allowed_gap_suffix(tmp_path: Path) -> None:
    path = tmp_path / "gap.csv"
    _write_csv(path, count=8, gap_at=3)
    prepared = prepare_candle_source(_config(path, gap_policy="allow"))
    complete = list(prepared.iter_candles())

    assert prepared.validation.gaps == 1
    assert list(prepared.iter_candles_from(3)) == complete[3:]
