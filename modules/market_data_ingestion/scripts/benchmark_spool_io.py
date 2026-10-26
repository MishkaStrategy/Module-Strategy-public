#!/usr/bin/env python3
"""Measure deterministic RecordSpool I/O, validation, and typed reread cost."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import platform
import resource
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import perf_counter, process_time
from typing import Any

from market_data_ingestion.contracts import InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.spool import RecordSpool
from market_data_ingestion.validation import validate_sorted_records

_STEP = timedelta(minutes=5)
_CLOSE_OFFSET = _STEP - timedelta(milliseconds=1)
_START = datetime(2020, 1, 1, tzinfo=UTC)


def peak_rss_bytes() -> int:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return int(value if platform.system() == "Darwin" else value * 1024)


def current_rss_bytes() -> int:
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


def process_io() -> dict[str, int]:
    path = Path("/proc/self/io")
    if not path.is_file():
        return {}
    values: dict[str, int] = {}
    for line in path.read_text(encoding="ascii").splitlines():
        name, raw = line.split(":", 1)
        values[name] = int(raw.strip())
    return values


def io_delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {
        name: max(0, after.get(name, 0) - value) for name, value in before.items() if name in after
    }


def storage_bytes(path: Path) -> int:
    return sum(
        candidate.stat().st_size
        for candidate in (
            path,
            Path(f"{path}-wal"),
            Path(f"{path}-shm"),
        )
        if candidate.is_file()
    )


def git_sha() -> str | None:
    value = os.environ.get("GITHUB_SHA")
    if value:
        return value
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def generate_records(offset: int, count: int) -> list[KlineRecord]:
    output: list[KlineRecord] = []
    for index in range(offset, offset + count):
        open_time = _START + index * _STEP
        base = 10_000 + index % 1_000
        output.append(
            KlineRecord(
                open_time=open_time,
                close_time=open_time + _CLOSE_OFFSET,
                open=str(base),
                high=str(base + 3),
                low=str(base - 2),
                close=str(base + 1),
                volume=str(100 + index % 50),
                quote_volume=str((100 + index % 50) * (base + 1)),
                trade_count=100 + index % 25,
                taker_buy_base_volume=str(50 + index % 20),
                taker_buy_quote_volume=str((50 + index % 20) * (base + 1)),
                symbol="BTCUSDT",
                timeframe="5m",
                exchange_id="binance",
                market_type=MarketType.USDT_M_PERPETUAL.value,
                source_kind="synthetic_benchmark",
                is_closed=True,
            )
        )
    return output


def table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (name,),
        ).fetchone()
        is not None
    )


def run(
    *,
    rows: int,
    source_batch_size: int,
    spool_batch_size: int,
    workdir: Path,
    cache_condition: str,
) -> dict[str, Any]:
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    spool_path = workdir / "record-spool.sqlite3"
    constructor_parameters = inspect.signature(RecordSpool).parameters
    spool_kwargs: dict[str, object] = {"reset": True}
    if "batch_size" in constructor_parameters:
        spool_kwargs["batch_size"] = spool_batch_size

    rss_start = current_rss_bytes()
    rss_peak_sampled = rss_start
    temp_peak = 0
    io_start = process_io()
    cpu_started = process_time()
    wall_started = perf_counter()
    phases: dict[str, float] = {}

    spool = RecordSpool(spool_path, **spool_kwargs)
    try:
        phase_started = perf_counter()
        for offset in range(0, rows, source_batch_size):
            batch = generate_records(offset, min(source_batch_size, rows - offset))
            spool.append("acquired", batch)
            del batch
            rss_peak_sampled = max(rss_peak_sampled, current_rss_bytes())
            temp_peak = max(temp_peak, storage_bytes(spool_path))
        phases["source_generate_and_spool_seconds"] = perf_counter() - phase_started

        accepted: list[KlineRecord] = []

        def accept(record: KlineRecord) -> None:
            accepted.append(record)
            if len(accepted) >= 1_000:
                spool.append("validated", accepted)
                accepted.clear()

        phase_started = perf_counter()
        validation = validate_sorted_records(
            spool.iter_sorted("acquired", DataType.KLINES, "5m"),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            accept=accept,
            now=datetime(2035, 1, 1, tzinfo=UTC),
            expected_start=_START,
            expected_end=_START + rows * _STEP,
        )
        if accepted:
            spool.append("validated", accepted)
            accepted.clear()
        promoted_rows = spool.count("validated", DataType.KLINES, "5m")
        phases["validation_and_promotion_seconds"] = perf_counter() - phase_started
        rss_peak_sampled = max(rss_peak_sampled, current_rss_bytes())
        temp_peak = max(temp_peak, storage_bytes(spool_path))

        if validation.status is not ValidationStatus.VALID:
            raise RuntimeError(
                f"validation status is {validation.status.value}: {validation.issues!r}"
            )
        if validation.gaps or validation.duplicates or validation.issues:
            raise RuntimeError("synthetic corpus unexpectedly produced validation evidence")

        phase_started = perf_counter()
        reread_rows = 0
        digest = hashlib.sha256()
        previous: datetime | None = None
        for record in spool.iter_sorted("validated", DataType.KLINES, "5m"):
            if previous is not None and record.open_time - previous != _STEP:
                raise RuntimeError("validated typed reread continuity changed")
            previous = record.open_time
            digest.update(
                (
                    f"{record.open_time.isoformat()}|{record.open}|{record.high}|"
                    f"{record.low}|{record.close}|{record.volume}\n"
                ).encode()
            )
            reread_rows += 1
            if reread_rows % source_batch_size == 0:
                rss_peak_sampled = max(rss_peak_sampled, current_rss_bytes())
        phases["validated_typed_reread_seconds"] = perf_counter() - phase_started
        temp_peak = max(temp_peak, storage_bytes(spool_path))

        physical_rows = int(spool._connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        direct_validated_rows = int(
            spool._connection.execute(
                "SELECT COUNT(*) FROM records WHERE stage = 'validated'"
            ).fetchone()[0]
        )
        validation_links = (
            int(spool._connection.execute("SELECT COUNT(*) FROM validated_records").fetchone()[0])
            if table_exists(spool._connection, "validated_records")
            else 0
        )
        acquired_rows = spool.count("acquired", DataType.KLINES, "5m")
        validated_rows = promoted_rows
        spool_bytes = storage_bytes(spool_path)
    finally:
        spool.close(remove=False)

    wall_seconds = perf_counter() - wall_started
    cpu_seconds = process_time() - cpu_started
    io_end = process_io()
    rss_peak_sampled = max(rss_peak_sampled, current_rss_bytes(), peak_rss_bytes())

    if acquired_rows != rows or validated_rows != rows or reread_rows != rows:
        raise RuntimeError(
            "row-count contract failed: "
            f"acquired={acquired_rows}, validated={validated_rows}, reread={reread_rows}"
        )

    result: dict[str, Any] = {
        "benchmark_version": 1,
        "benchmark_kind": "record_spool_validation_io",
        "git_sha": git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cache_condition": cache_condition,
        "corpus": {
            "kind": "deterministic_synthetic_btcusdt_5m",
            "symbol": "BTCUSDT",
            "market_type": MarketType.USDT_M_PERPETUAL.value,
            "timeframe": "5m",
            "start": _START.isoformat(),
            "end_exclusive": (_START + rows * _STEP).isoformat(),
            "rows": rows,
            "source_batch_size": source_batch_size,
        },
        "spool": {
            "configured_batch_size": (
                spool_batch_size if "batch_size" in constructor_parameters else 1_000
            ),
            "supports_configurable_batch_size": "batch_size" in constructor_parameters,
            "physical_record_rows": physical_rows,
            "direct_validated_payload_rows": direct_validated_rows,
            "validation_link_rows": validation_links,
            "serialized_payload_copies_per_logical_row": round(physical_rows / rows, 6),
            "bytes_at_completion": spool_bytes,
            "peak_sampled_temporary_bytes": temp_peak,
        },
        "validation": {
            "status": validation.status.value,
            "rows_checked": validation.rows_checked,
            "issues": len(validation.issues),
            "gaps": len(validation.gaps),
            "duplicates": len(validation.duplicates),
            "typed_reread_rows": reread_rows,
            "typed_reread_digest_sha256": digest.hexdigest(),
        },
        "performance": {
            "wall_seconds": round(wall_seconds, 6),
            "cpu_seconds": round(cpu_seconds, 6),
            "rows_per_second": round(rows / wall_seconds, 3),
            "phases_seconds": {name: round(value, 6) for name, value in phases.items()},
        },
        "memory": {
            "rss_start_bytes": rss_start,
            "rss_peak_sampled_bytes": rss_peak_sampled,
            "rss_peak_delta_bytes": max(0, rss_peak_sampled - rss_start),
        },
        "io": {
            "process_counters_available": bool(io_start and io_end),
            "process_counter_delta": io_delta(io_start, io_end),
            "logical_full_spool_scans": 2,
            "file_open_count_available": False,
        },
        "gate": {
            "passed": True,
            "criteria": {
                "all_rows_acquired": acquired_rows == rows,
                "all_rows_validated": validated_rows == rows,
                "all_rows_typed_reread": reread_rows == rows,
                "validation_valid": validation.status is ValidationStatus.VALID,
                "zero_gaps_duplicates_issues": not (
                    validation.gaps or validation.duplicates or validation.issues
                ),
            },
        },
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=20_000)
    parser.add_argument("--source-batch-size", type=int, default=8_192)
    parser.add_argument("--spool-batch-size", type=int, default=8_192)
    parser.add_argument("--cache-condition", default="warm-process-isolated")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    for name in ("rows", "source_batch_size", "spool_batch_size"):
        value = getattr(args, name)
        if value <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")

    temporary = args.workdir is None
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="mdi-spool-benchmark-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = run(
            rows=args.rows,
            source_batch_size=args.source_batch_size,
            spool_batch_size=args.spool_batch_size,
            workdir=workdir.resolve(),
            cache_condition=args.cache_condition,
        )
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    finally:
        if temporary and not args.keep_data:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
