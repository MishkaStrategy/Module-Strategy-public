from __future__ import annotations

import argparse
import gc
import json
import tempfile
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter

import pyarrow as pa
import pyarrow.parquet as pq

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.enums import MarketType
from market_data_ingestion.external_corpus import ExternalCorpusFile, ExternalCorpusSpec
from market_data_ingestion.external_registration import ExternalCorpusRegistrar
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from modular_backtest.config import DataSourceConfig
from modular_backtest.data import load_validated_candles
from modular_backtest.registered_data import load_registered_candles, open_registered_candles


def _write_source(path: Path, rows: int) -> tuple[datetime, datetime]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    step = timedelta(minutes=5)
    open_times = [start + index * step for index in range(rows)]
    indices = range(rows)
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [10_000.0 + index * 0.01 for index in indices],
                "high": [10_001.0 + index * 0.01 for index in range(rows)],
                "low": [9_999.0 + index * 0.01 for index in range(rows)],
                "close": [10_000.5 + index * 0.01 for index in range(rows)],
                "volume": [100.0 + index % 7 for index in range(rows)],
                "close_time": [item + step - timedelta(milliseconds=1) for item in open_times],
                "quote_volume": [1_000_000.0 + index for index in range(rows)],
                "trades": [100 + index % 23 for index in range(rows)],
                "taker_buy_base": [50.0 + index % 5 for index in range(rows)],
                "taker_buy_quote": [500_000.0 + index for index in range(rows)],
            }
        ),
        path,
        compression="zstd",
    )
    return open_times[0], open_times[-1]


def _register_fixture(root: Path, rows: int) -> DataSourceConfig:
    source = root / "BTCUSDT_5m.parquet"
    first, last = _write_source(source, rows)
    spec = ExternalCorpusSpec(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        required_timeframes=("5m",),
        files=(
            ExternalCorpusFile(
                symbol="BTCUSDT",
                timeframe="5m",
                path=source,
                source_uri="local-corpus://benchmark/btcusdt/5m",
                expected_rows=rows,
                expected_first_open_time=first,
                expected_last_open_time=last,
                expected_sha256=sha256_file(source),
            ),
        ),
    )
    storage = LocalStorage(root / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = ExternalCorpusRegistrar(storage=storage, registry=registry).register(spec)
    descriptor = result.datasets[0].descriptor
    return DataSourceConfig(
        symbol="BTCUSDT",
        storage_root=storage.root,
        dataset_id=descriptor.dataset_id,
        dataset_version=descriptor.version,
    )


def run(rows: int, mode: str) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="registered-data-benchmark-") as temporary:
        config = _register_fixture(Path(temporary), rows)
        gc.collect()
        tracemalloc.start()
        started = perf_counter()
        first_candle_seconds: float | None = None

        if mode == "validated":
            candles, summary = load_validated_candles(config)
            dataset_hash = summary.dataset_hash
        elif mode == "compatibility":
            batch = load_registered_candles(config)
            candles = list(batch.candles)
            dataset_hash = batch.dataset_hash
        else:
            source = open_registered_candles(config)
            iterator = source.candles
            first = next(iterator)
            first_candle_seconds = perf_counter() - started
            candles = [first, *iterator]
            dataset_hash = source.dataset_hash

        elapsed = perf_counter() - started
        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        return {
            "mode": mode,
            "rows": len(candles),
            "elapsed_seconds": elapsed,
            "rows_per_second": len(candles) / elapsed,
            "first_candle_seconds": first_candle_seconds,
            "peak_memory_bytes": peak,
            "dataset_id": config.dataset_id,
            "dataset_version": config.dataset_version,
            "dataset_hash": dataset_hash,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, default=100_000)
    parser.add_argument(
        "--mode",
        choices=("source", "validated", "compatibility"),
        default="validated",
    )
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.rows <= 0:
        parser.error("--rows must be positive")

    payload = run(args.rows, args.mode)
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
