#!/usr/bin/env python3
"""Reproducible bounded-memory ingestion benchmark using the deterministic fake adapter."""

from __future__ import annotations

import argparse
import json
import platform
import resource
import shutil
import sys
import tempfile
import tracemalloc
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter
from typing import Any

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.version import NORMALIZATION_VERSION, PACKAGE_VERSION


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("value must be positive")
    return parsed


def _directory_size(path: Path) -> int:
    return sum(
        item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink()
    )


def _rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports KiB; macOS reports bytes.
    return int(value if sys.platform == "darwin" else value * 1024)


def run_benchmark(
    *,
    bars: int,
    chunk_bars: int,
    root: Path,
    verify: bool,
) -> dict[str, Any]:
    if chunk_bars > bars:
        raise ValueError("chunk_bars must not exceed bars")
    storage = LocalStorage(root / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    start = datetime(2024, 1, 1, tzinfo=UTC)
    request = MarketDataRequest(
        request_id=f"benchmark-{bars}-{chunk_bars}",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=start,
        end=start + timedelta(minutes=5 * bars),
    )
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter(chunk_bars=chunk_bars)},
        storage=storage,
        registry=registry,
    )

    tracemalloc.start()
    started = perf_counter()
    result = service.execute(
        request,
        format_name="csv",
        concurrency=1,
        max_records_in_memory=chunk_bars,
    )
    elapsed = perf_counter() - started
    _, peak_python_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    if result.dataset is None:
        raise RuntimeError("benchmark ingestion did not register a dataset")

    manifest_path = storage.root / result.dataset.manifest_uri
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    partitions = manifest["partitions"]
    base_rows = sum(
        int(item["row_count"])
        for item in partitions
        if item["data_type"] == "klines" and item["timeframe"] == "5m"
    )
    integrity_seconds: float | None = None
    integrity_status: str | None = None
    if verify:
        verified_started = perf_counter()
        payload = registry.inspect(result.dataset.dataset_id, result.dataset.version)
        if payload is None:
            raise RuntimeError("registered benchmark dataset disappeared")
        integrity_status = str(verify_registered_dataset(storage.root, payload)["status"])
        integrity_seconds = perf_counter() - verified_started

    layer_sizes = {
        layer: _directory_size(storage.root / layer)
        for layer in ("raw", "normalized", "derived", "manifests")
    }
    return {
        "benchmark_version": 1,
        "package_version": PACKAGE_VERSION,
        "normalization_version": NORMALIZATION_VERSION,
        "python": platform.python_version(),
        "platform": platform.platform(),
        "bars_requested": bars,
        "base_rows_registered": base_rows,
        "chunk_bars": chunk_bars,
        "adapter_chunks": (bars + chunk_bars - 1) // chunk_bars,
        "elapsed_seconds": round(elapsed, 6),
        "base_rows_per_second": round(base_rows / elapsed, 3),
        "peak_python_bytes": peak_python_bytes,
        "peak_rss_bytes": _rss_bytes(),
        "dataset_id": result.dataset.dataset_id,
        "dataset_version": result.dataset.version,
        "dataset_hash": result.dataset.dataset_hash,
        "partition_count": len(partitions),
        "raw_artifact_count": len(manifest["source_artifacts"]),
        "layer_bytes": layer_sizes,
        "total_storage_bytes": sum(layer_sizes.values())
        + (storage.root / "registry.sqlite3").stat().st_size,
        "integrity_status": integrity_status,
        "integrity_seconds": (
            round(integrity_seconds, 6) if integrity_seconds is not None else None
        ),
        "staging_files_after_completion": sorted(
            item.name for item in (storage.root / "staging").glob("*")
        ),
        "completed_at": datetime.now(UTC).isoformat(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bars", type=_positive_int, default=100_000)
    parser.add_argument("--chunk-bars", type=_positive_int, default=1_000)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--skip-integrity", action="store_true")
    parser.add_argument("--keep-data", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    temporary = args.workdir is None
    root = (
        Path(tempfile.mkdtemp(prefix="mdi-streaming-benchmark-"))
        if temporary
        else args.workdir.expanduser().resolve()
    )
    root.mkdir(parents=True, exist_ok=True)
    try:
        result = run_benchmark(
            bars=args.bars,
            chunk_bars=args.chunk_bars,
            root=root,
            verify=not args.skip_integrity,
        )
        encoded = json.dumps(result, indent=2, sort_keys=True) + "\n"
        if args.output is not None:
            output = args.output.expanduser().resolve()
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(encoded, encoding="utf-8")
        sys.stdout.write(encoded)
        return 0
    finally:
        if temporary and not args.keep_data:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
