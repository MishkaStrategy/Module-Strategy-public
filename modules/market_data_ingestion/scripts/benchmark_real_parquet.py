#!/usr/bin/env python3
"""Benchmark real multi-year BTCUSDT Parquet through the bounded-memory pipeline."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import resource
import shutil
import subprocess
import tempfile
import tracemalloc
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path
from time import perf_counter
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq

from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.contracts import InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.spool import RecordSpool
from market_data_ingestion.storage import LocalStorage
from market_data_ingestion.validation import validate_sorted_records
from market_data_ingestion.version import PACKAGE_VERSION

STEP_MS = 5 * 60 * 1000
COLUMNS = (
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
)


def directory_size(path: Path) -> int:
    if not path.exists():
        return 0
    return sum(
        item.stat().st_size for item in path.rglob("*") if item.is_file() and not item.is_symlink()
    )


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if platform.system() == "Darwin" else value * 1024)


def current_rss_bytes() -> int:
    """Return current resident memory using only the Python standard library."""
    statm = Path("/proc/self/statm")
    if statm.exists():
        fields = statm.read_text(encoding="ascii").split()
        if len(fields) >= 2:
            return int(fields[1]) * int(os.sysconf("SC_PAGE_SIZE"))
    if platform.system() == "Darwin":
        output = subprocess.check_output(
            ["ps", "-o", "rss=", "-p", str(os.getpid())],
            text=True,
        ).strip()
        return int(output) * 1024
    return peak_rss_bytes()


class MemorySampler:
    def __init__(self) -> None:
        self.start_rss = current_rss_bytes()
        self.peak_sampled_rss = self.start_rss
        self.peak_arrow_bytes = pa.total_allocated_bytes()

    def sample(self) -> None:
        self.peak_sampled_rss = max(self.peak_sampled_rss, current_rss_bytes())
        self.peak_arrow_bytes = max(self.peak_arrow_bytes, pa.total_allocated_bytes())


def record_batches(path: Path, batch_size: int):
    parquet = pq.ParquetFile(path)
    observed = tuple(parquet.schema_arrow.names)
    if observed != COLUMNS:
        raise RuntimeError(f"unexpected source schema: {observed!r}")
    yield from parquet.iter_batches(batch_size=batch_size, columns=list(COLUMNS))


def batch_records(
    batch: pa.RecordBatch,
    instrument: InstrumentReference,
) -> list[KlineRecord]:
    data = batch.to_pydict()
    output: list[KlineRecord] = []
    for index in range(batch.num_rows):
        output.append(
            KlineRecord(
                open_time=data["open_time"][index],
                close_time=data["close_time"][index],
                open=str(data["open"][index]),
                high=str(data["high"][index]),
                low=str(data["low"][index]),
                close=str(data["close"][index]),
                volume=str(data["volume"][index]),
                quote_volume=str(data["quote_volume"][index]),
                trade_count=int(data["trades"][index]),
                taker_buy_base_volume=str(data["taker_buy_base"][index]),
                taker_buy_quote_volume=str(data["taker_buy_quote"][index]),
                symbol=instrument.symbol,
                timeframe="5m",
                exchange_id=instrument.exchange_id,
                market_type=instrument.market_type.value,
                source_kind="verified_external_parquet",
                is_closed=True,
            )
        )
    return output


def run(
    input_path: Path,
    output_path: Path,
    batch_size: int,
    workdir: Path,
) -> dict[str, Any]:
    started_total = perf_counter()
    sampler = MemorySampler()
    tracemalloc.start()

    input_sha256 = sha256_file(input_path)
    input_size = input_path.stat().st_size
    parquet = pq.ParquetFile(input_path)
    row_count_metadata = parquet.metadata.num_rows
    row_group_count = parquet.metadata.num_row_groups

    storage = LocalStorage(workdir / "data")
    spool_path = workdir / "record-spool.sqlite3"
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )

    phases: dict[str, float] = {}
    source_rows = 0
    source_batches = 0
    first_open: datetime | None = None
    last_open: datetime | None = None

    with RecordSpool(spool_path, reset=True) as spool:
        started = perf_counter()
        for batch in record_batches(input_path, batch_size):
            records = batch_records(batch, instrument)
            if records:
                first_open = first_open or records[0].open_time
                last_open = records[-1].open_time
            spool.append("acquired", records)
            source_rows += len(records)
            source_batches += 1
            del records
            sampler.sample()
        phases["source_decode_and_spool_seconds"] = perf_counter() - started

        if first_open is None or last_open is None:
            raise RuntimeError("source produced no records")
        if source_rows != row_count_metadata:
            raise RuntimeError(f"metadata rows {row_count_metadata} != decoded rows {source_rows}")

        accepted_buffer: list[KlineRecord] = []

        def accept(record: KlineRecord) -> None:
            accepted_buffer.append(record)
            if len(accepted_buffer) >= 1_000:
                spool.append("validated", accepted_buffer)
                accepted_buffer.clear()
                sampler.sample()

        started = perf_counter()
        validation = validate_sorted_records(
            spool.iter_sorted("acquired", DataType.KLINES, "5m"),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            accept=accept,
            now=datetime(2027, 1, 1, tzinfo=UTC),
            expected_start=first_open,
            expected_end=last_open + timedelta(minutes=5),
        )
        if accepted_buffer:
            spool.append("validated", accepted_buffer)
            accepted_buffer.clear()
        phases["stream_validation_seconds"] = perf_counter() - started
        sampler.sample()

        if validation.status is not ValidationStatus.VALID:
            raise RuntimeError(
                f"validation status is {validation.status.value}: {validation.issues}"
            )
        if validation.gaps or validation.duplicates or validation.issues:
            raise RuntimeError("real dataset validation unexpectedly produced evidence")

        started = perf_counter()
        partitions = []
        maximum_month_rows = 0
        source_id = f"raw:{input_sha256[:24]}"
        stream = spool.iter_sorted("validated", DataType.KLINES, "5m")
        month_groups = groupby(
            stream,
            key=lambda item: (item.timestamp.year, item.timestamp.month),
        )
        for _, month_group in month_groups:
            month_records = tuple(month_group)
            maximum_month_rows = max(maximum_month_rows, len(month_records))
            descriptor = storage.write_partition(
                dataset_id="benchmark_real_btcusdt_5m",
                version=1,
                layer="normalized",
                instrument=instrument,
                data_type=DataType.KLINES,
                timeframe="5m",
                records=month_records,
                source_artifact_ids=(source_id,),
                format_name="parquet",
                compression="zstd",
            )
            partitions.append(descriptor)
            del month_records
            sampler.sample()
        phases["monthly_parquet_publish_seconds"] = perf_counter() - started

        started = perf_counter()
        reread_rows = 0
        reread_first: datetime | None = None
        reread_last: datetime | None = None
        previous_ms: int | None = None
        gap_count = 0
        duplicate_count = 0
        out_of_order_count = 0
        digest = hashlib.sha256()
        for descriptor in partitions:
            for record in storage.iter_partition_records(descriptor):
                timestamp_ms = int(record.open_time.timestamp() * 1000)
                if previous_ms is not None:
                    delta = timestamp_ms - previous_ms
                    if delta == 0:
                        duplicate_count += 1
                    elif delta < 0:
                        out_of_order_count += 1
                    elif delta > STEP_MS:
                        gap_count += delta // STEP_MS - 1
                previous_ms = timestamp_ms
                reread_first = reread_first or record.open_time
                reread_last = record.open_time
                digest.update(
                    (
                        f"{record.open_time.isoformat()}|{record.open}|{record.high}|"
                        f"{record.low}|{record.close}|{record.volume}\n"
                    ).encode()
                )
                reread_rows += 1
                if reread_rows % batch_size == 0:
                    sampler.sample()
        phases["checksum_verified_typed_reread_seconds"] = perf_counter() - started
        sampler.sample()

        if reread_rows != source_rows:
            raise RuntimeError(f"reread rows {reread_rows} != source rows {source_rows}")
        if (gap_count, duplicate_count, out_of_order_count) != (0, 0, 0):
            raise RuntimeError("reread continuity failed")
        if reread_first != first_open or reread_last != last_open:
            raise RuntimeError("reread coverage differs from source")

        acquired_count = spool.count("acquired", DataType.KLINES, "5m")
        validated_count = spool.count("validated", DataType.KLINES, "5m")

    _, peak_python_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    total_seconds = perf_counter() - started_total
    sampler.sample()

    normalized_bytes = directory_size(storage.root / "normalized")
    result: dict[str, Any] = {
        "benchmark_version": 1,
        "benchmark_kind": "multi_year_real_parquet_normalization_validation_publish_reread",
        "package_version": PACKAGE_VERSION,
        "python": platform.python_version(),
        "pyarrow": pa.__version__,
        "platform": platform.platform(),
        "input": {
            "name": input_path.name,
            "sha256": input_sha256,
            "size_bytes": input_size,
            "parquet_row_groups": row_group_count,
            "schema": str(parquet.schema_arrow),
        },
        "range": {
            "start": first_open.isoformat(),
            "last_open": last_open.isoformat(),
            "exclusive_end": (last_open + timedelta(minutes=5)).isoformat(),
        },
        "rows": {
            "metadata": row_count_metadata,
            "decoded": source_rows,
            "spooled_acquired": acquired_count,
            "spooled_validated": validated_count,
            "typed_reread": reread_rows,
        },
        "streaming": {
            "source_batch_size": batch_size,
            "source_batch_count": source_batches,
            "monthly_partition_count": len(partitions),
            "maximum_month_rows_materialized": maximum_month_rows,
        },
        "validation": {
            "status": validation.status.value,
            "rows_checked": validation.rows_checked,
            "issues": len(validation.issues),
            "gaps": len(validation.gaps),
            "duplicates": len(validation.duplicates),
            "reread_gap_intervals": gap_count,
            "reread_duplicates": duplicate_count,
            "reread_out_of_order": out_of_order_count,
            "typed_reread_digest_sha256": digest.hexdigest(),
        },
        "performance": {
            "total_seconds": round(total_seconds, 6),
            "rows_per_second_total": round(source_rows / total_seconds, 3),
            "phases_seconds": {name: round(value, 6) for name, value in phases.items()},
        },
        "memory": {
            "rss_start_bytes": sampler.start_rss,
            "rss_peak_sampled_bytes": sampler.peak_sampled_rss,
            "rss_peak_resource_bytes": peak_rss_bytes(),
            "rss_peak_delta_bytes": max(
                0,
                sampler.peak_sampled_rss - sampler.start_rss,
            ),
            "python_heap_peak_bytes": peak_python_bytes,
            "arrow_peak_allocated_bytes": sampler.peak_arrow_bytes,
        },
        "storage": {
            "source_bytes": input_size,
            "spool_bytes": spool_path.stat().st_size if spool_path.exists() else 0,
            "normalized_partition_bytes": normalized_bytes,
            "partition_rows_sum": sum(item.row_count for item in partitions),
            "partition_checksums_unique": len({item.checksum_sha256 for item in partitions}),
        },
        "gate": {
            "passed": True,
            "criteria": {
                "all_rows_processed": reread_rows == row_count_metadata,
                "validation_valid": validation.status is ValidationStatus.VALID,
                "zero_gaps_duplicates_order_errors": (
                    gap_count == duplicate_count == out_of_order_count == 0
                ),
                "monthly_memory_bound": maximum_month_rows < 10_000,
                "rss_peak_below_1_gib": peak_rss_bytes() < 1024**3,
            },
        },
        "completed_at": datetime.now(UTC).isoformat(),
    }
    criteria = result["gate"]["criteria"]
    if not all(criteria.values()):
        result["gate"]["passed"] = False
        raise RuntimeError(f"benchmark gate failed: {criteria}")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--batch-size", type=int, default=8_192)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    parser.add_argument("--expected-sha256")
    args = parser.parse_args()
    temporary = args.workdir is None
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="mdi-real-benchmark-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        input_path = args.input.resolve()
        if args.expected_sha256 is not None:
            observed = sha256_file(input_path)
            if observed != args.expected_sha256.lower():
                raise SystemExit(
                    "input SHA-256 mismatch: "
                    f"expected {args.expected_sha256.lower()}, observed {observed}"
                )
        result = run(
            input_path,
            args.output.resolve(),
            args.batch_size,
            workdir.resolve(),
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if temporary and not args.keep_data:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
