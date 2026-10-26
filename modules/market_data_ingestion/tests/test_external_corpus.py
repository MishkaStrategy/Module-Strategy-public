from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.enums import MarketType
from market_data_ingestion.errors import StorageError, StructuredIngestionError
from market_data_ingestion.external_corpus import (
    ExternalCorpusFile,
    ExternalCorpusSpec,
    ExternalCorpusVerifier,
    load_external_corpus_spec,
    write_external_corpus_manifest,
)


def _write_klines(
    path: Path, *, timeframe: str, count: int, gap_at: int | None = None
) -> tuple[datetime, datetime]:
    minutes = {"5m": 5, "10m": 10}[timeframe]
    step = timedelta(minutes=minutes)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    open_times = []
    for index in range(count):
        offset = index + (1 if gap_at is not None and index >= gap_at else 0)
        open_times.append(start + offset * step)
    close_times = [item + step - timedelta(milliseconds=1) for item in open_times]
    columns = {
        "open_time": open_times,
        "open": [100.0 + index for index in range(count)],
        "high": [101.0 + index for index in range(count)],
        "low": [99.0 + index for index in range(count)],
        "close": [100.5 + index for index in range(count)],
        "volume": [10.0 + index for index in range(count)],
        "close_time": close_times,
        "quote_volume": [1000.0 + index for index in range(count)],
        "trades": [10 + index for index in range(count)],
        "taker_buy_base": [5.0 + index for index in range(count)],
        "taker_buy_quote": [500.0 + index for index in range(count)],
    }
    pq.write_table(pa.table(columns), path)
    return open_times[0], open_times[-1]


def _source(
    path: Path, timeframe: str, rows: int, first: datetime, last: datetime
) -> ExternalCorpusFile:
    return ExternalCorpusFile(
        symbol="BTCUSDT",
        timeframe=timeframe,
        path=path,
        source_uri=f"gdrive://{path.stem}",
        expected_rows=rows,
        expected_first_open_time=first,
        expected_last_open_time=last,
        expected_sha256=sha256_file(path),
    )


def test_external_corpus_verification_and_manifest_are_content_addressed(tmp_path: Path) -> None:
    five = tmp_path / "BTCUSDT_5m.parquet"
    ten = tmp_path / "BTCUSDT_10m.parquet"
    five_first, five_last = _write_klines(five, timeframe="5m", count=6)
    ten_first, ten_last = _write_klines(ten, timeframe="10m", count=3)
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m", "10m"),
        files=(
            _source(five, "5m", 6, five_first, five_last),
            _source(ten, "10m", 3, ten_first, ten_last),
        ),
    )

    first = ExternalCorpusVerifier().verify(spec)
    second = ExternalCorpusVerifier().verify(spec)

    assert first.corpus_hash == second.corpus_hash
    assert len(first.files) == 2
    assert all(item.gap_events == 0 for item in first.files)
    assert all(item.duplicate_events == 0 for item in first.files)
    target = tmp_path / "manifest.json"
    assert write_external_corpus_manifest(target, first) == target
    assert write_external_corpus_manifest(target, second) == target
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["corpus_hash"] == first.corpus_hash


def test_external_corpus_rejects_checksum_mismatch(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_5m.parquet"
    first, last = _write_klines(path, timeframe="5m", count=2)
    source = ExternalCorpusFile(
        symbol="BTCUSDT",
        timeframe="5m",
        path=path,
        source_uri="gdrive://btc-5m",
        expected_rows=2,
        expected_first_open_time=first,
        expected_last_open_time=last,
        expected_sha256="0" * 64,
    )
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(source,),
    )

    with pytest.raises(StructuredIngestionError, match="checksum"):
        ExternalCorpusVerifier().verify(spec)


def test_external_corpus_rejects_summary_gap_arithmetic(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_5m.parquet"
    first, last = _write_klines(path, timeframe="5m", count=5, gap_at=2)
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(_source(path, "5m", 5, first, last),),
    )

    with pytest.raises(StructuredIngestionError, match="summary"):
        ExternalCorpusVerifier().verify(spec)


def test_external_corpus_rejects_schema_drift(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_5m.parquet"
    pq.write_table(pa.table({"open_time": [datetime(2024, 1, 1, tzinfo=UTC)]}), path)
    source = ExternalCorpusFile(
        symbol="BTCUSDT",
        timeframe="5m",
        path=path,
        source_uri="gdrive://btc-schema",
        expected_rows=1,
        expected_first_open_time=datetime(2024, 1, 1, tzinfo=UTC),
        expected_last_open_time=datetime(2024, 1, 1, tzinfo=UTC),
    )
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(source,),
    )

    with pytest.raises(StructuredIngestionError, match="columns"):
        ExternalCorpusVerifier().verify(spec)


def test_load_external_corpus_spec_is_strict(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_5m.parquet"
    first, last = _write_klines(path, timeframe="5m", count=2)
    spec_path = tmp_path / "spec.json"
    spec_path.write_text(
        json.dumps(
            {
                "spec_version": 1,
                "exchange_id": "binance",
                "market_type": "usdt_m_perpetual",
                "required_timeframes": ["5m"],
                "max_file_bytes": 1024 * 1024,
                "files": [
                    {
                        "symbol": "BTCUSDT",
                        "timeframe": "5m",
                        "path": str(path),
                        "source_uri": "gdrive://btc-load",
                        "expected_rows": 2,
                        "expected_first_open_time": first.isoformat(),
                        "expected_last_open_time": last.isoformat(),
                        "expected_sha256": sha256_file(path),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    loaded = load_external_corpus_spec(spec_path)
    assert loaded.files[0].path == path
    assert loaded.market_type is MarketType.USDT_M_PERPETUAL


def test_manifest_writer_rejects_different_identity(tmp_path: Path) -> None:
    path = tmp_path / "BTCUSDT_5m.parquet"
    first, last = _write_klines(path, timeframe="5m", count=2)
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(_source(path, "5m", 2, first, last),),
    )
    manifest = ExternalCorpusVerifier().verify(spec)
    target = tmp_path / "manifest.json"
    write_external_corpus_manifest(target, manifest)
    payload = json.loads(target.read_text(encoding="utf-8"))
    payload["corpus_hash"] = "0" * 64
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(StorageError, match="different content"):
        write_external_corpus_manifest(target, manifest)
