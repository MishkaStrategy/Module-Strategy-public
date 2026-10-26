from __future__ import annotations

import argparse
import gc
import hashlib
import json
import resource
import sys
import tempfile
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter, process_time
from typing import Protocol

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
from modular_backtest.registered_data import (
    load_registered_candles,
    open_streaming_registered_candles,
)
from modular_core.models import Candle


class _Digest(Protocol):
    def update(self, data: bytes) -> object: ...

    def hexdigest(self) -> str: ...


def _process_peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if sys.platform == "darwin" else value * 1024)


def _current_rss_bytes() -> int:
    try:
        import psutil  # type: ignore[import-untyped]
    except ImportError:
        return _process_peak_rss_bytes()
    return int(psutil.Process().memory_info().rss)


def _arrow_peak_bytes() -> int:
    memory_pool = pa.default_memory_pool()
    maximum = getattr(memory_pool, "max_memory", None)
    if callable(maximum):
        return int(maximum())
    return int(pa.total_allocated_bytes())


def _count_candles() -> int:
    return sum(1 for value in gc.get_objects() if isinstance(value, Candle))


def _write_source(path: Path, rows: int) -> tuple[datetime, datetime]:
    start = datetime(2020, 1, 1, tzinfo=UTC)
    step = timedelta(minutes=5)
    open_times = [start + index * step for index in range(rows)]
    pq.write_table(
        pa.table(
            {
                "open_time": open_times,
                "open": [10_000.0 + index * 0.01 for index in range(rows)],
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


def _hash_candle(digest: _Digest, candle: Candle) -> None:
    digest.update(candle.timestamp.isoformat().encode("ascii"))
    digest.update(b"|")
    for value in (candle.open, candle.high, candle.low, candle.close, candle.volume):
        digest.update(format(value, ".17g").encode("ascii"))
        digest.update(b"|")
    digest.update(candle.symbol.encode("ascii"))
    digest.update(b"|")
    digest.update(candle.timeframe.encode("ascii"))
    digest.update(b"\n")


def run_once(rows: int, mode: str, batch_size: int) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="registered-streaming-benchmark-") as temporary:
        config = _register_fixture(Path(temporary), rows)
        gc.collect()
        candles_before = _count_candles()
        rss_before = _current_rss_bytes()
        arrow_before = pa.total_allocated_bytes()
        arrow_peak_before = _arrow_peak_bytes()
        peak_rss = rss_before
        peak_arrow = arrow_before
        digest = hashlib.sha256()
        retained: list[Candle] | tuple[Candle, ...] | None = None
        source_open_count = 0
        partition_count: int | None = None
        first_candle_seconds: float | None = None
        rows_seen = 0
        full_dataset_copies = 0

        tracemalloc.start()
        wall_started = perf_counter()
        cpu_started = process_time()

        if mode == "legacy_materialized":
            candles, summary = load_validated_candles(config)
            retained = candles
            partition_count = summary.partition_count
            first_candle_seconds = perf_counter() - wall_started
            full_dataset_copies = 1
            for candle in candles:
                _hash_candle(digest, candle)
                rows_seen += 1
                if rows_seen % 4096 == 0:
                    peak_rss = max(peak_rss, _current_rss_bytes())
                    peak_arrow = max(peak_arrow, pa.total_allocated_bytes())
        elif mode == "legacy_tuple_compatibility":
            batch = load_registered_candles(config)
            retained = batch.candles
            partition_count = batch.partition_count
            first_candle_seconds = perf_counter() - wall_started
            source_open_count = 1
            full_dataset_copies = 1
            for candle in batch.candles:
                _hash_candle(digest, candle)
                rows_seen += 1
                if rows_seen % 4096 == 0:
                    peak_rss = max(peak_rss, _current_rss_bytes())
                    peak_arrow = max(peak_arrow, pa.total_allocated_bytes())
        elif mode == "streaming":
            source = open_streaming_registered_candles(config, batch_size=batch_size)
            partition_count = source.partition_count
            iterator = source.open()
            first = next(iterator)
            first_candle_seconds = perf_counter() - wall_started
            _hash_candle(digest, first)
            rows_seen = 1
            for candle in iterator:
                _hash_candle(digest, candle)
                rows_seen += 1
                if rows_seen % 4096 == 0:
                    peak_rss = max(peak_rss, _current_rss_bytes())
                    peak_arrow = max(peak_arrow, pa.total_allocated_bytes())
            source_open_count = source.source_open_count
            del first, iterator, source
        else:
            raise ValueError(f"unsupported mode: {mode}")

        wall_seconds = perf_counter() - wall_started
        cpu_seconds = process_time() - cpu_started
        _, tracemalloc_peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        peak_rss = max(peak_rss, _current_rss_bytes())
        peak_arrow = max(peak_arrow, pa.total_allocated_bytes())
        gc.collect()
        observed_candles = max(0, _count_candles() - candles_before)
        retained_candles = len(retained) if retained is not None else 0
        retained_container_bytes = sys.getsizeof(retained) if retained is not None else 0

        retained = None
        gc.collect()
        return {
            "mode": mode,
            "rows": rows_seen,
            "batch_size": batch_size if mode == "streaming" else None,
            "partition_count": partition_count,
            "wall_seconds": wall_seconds,
            "cpu_seconds": cpu_seconds,
            "rows_per_second": rows_seen / wall_seconds,
            "time_to_first_bar_seconds": first_candle_seconds,
            "rss_before_bytes": rss_before,
            "peak_sampled_rss_bytes": peak_rss,
            "tracemalloc_peak_bytes": tracemalloc_peak,
            "arrow_before_bytes": arrow_before,
            "peak_sampled_arrow_bytes": peak_arrow,
            "arrow_process_peak_delta_bytes": max(0, _arrow_peak_bytes() - arrow_peak_before),
            "observed_live_candle_objects": observed_candles,
            "retained_candle_objects": retained_candles,
            "retained_container_bytes": retained_container_bytes,
            "full_dataset_copies": full_dataset_copies,
            "source_open_count": source_open_count,
            "result_hash": digest.hexdigest(),
            "dataset_id": config.dataset_id,
            "dataset_version": config.dataset_version,
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rows", type=int, action="append", default=[])
    parser.add_argument(
        "--mode",
        action="append",
        choices=(
            "legacy_materialized",
            "legacy_tuple_compatibility",
            "streaming",
        ),
        default=[],
    )
    parser.add_argument("--batch-size", type=int, action="append", default=[])
    parser.add_argument("--output")
    args = parser.parse_args()

    row_counts = args.rows or [10_000]
    modes = args.mode or [
        "legacy_materialized",
        "legacy_tuple_compatibility",
        "streaming",
    ]
    batch_sizes = args.batch_size or [1, 1024, 8192]
    if any(value <= 0 for value in row_counts):
        parser.error("--rows values must be positive")
    if any(value <= 0 for value in batch_sizes):
        parser.error("--batch-size values must be positive")

    results: list[dict[str, object]] = []
    for rows in row_counts:
        for mode in modes:
            sizes = batch_sizes if mode == "streaming" else [batch_sizes[0]]
            for batch_size in sizes:
                results.append(run_once(rows, mode, batch_size))

    payload = {"schema_version": 1, "results": results}
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    print(rendered)
    if args.output:
        Path(args.output).write_text(rendered + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
