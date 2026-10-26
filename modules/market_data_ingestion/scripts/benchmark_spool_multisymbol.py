#!/usr/bin/env python3
"""Benchmark deterministic multi-symbol RecordSpool validation and reread I/O."""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import platform
import shutil
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter, process_time
from typing import Any

from benchmark_spool_io import (
    _START,
    _STEP,
    current_rss_bytes,
    generate_records,
    git_sha,
    peak_rss_bytes,
    process_io,
    storage_bytes,
)

from market_data_ingestion.contracts import InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.spool import RecordSpool
from market_data_ingestion.validation import validate_sorted_records

_DEFAULT_SYMBOLS = ("ADAUSDT", "BTCUSDT", "ETHUSDT")


def run_symbol(
    *,
    symbol: str,
    rows: int,
    source_batch_size: int,
    spool_batch_size: int,
    workdir: Path,
) -> dict[str, Any]:
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol=symbol,
        canonical_id=f"binance:usdt_m_perpetual:{symbol.lower()}",
    )
    spool_path = workdir / f"{symbol.lower()}-record-spool.sqlite3"
    constructor_parameters = inspect.signature(RecordSpool).parameters
    spool_kwargs: dict[str, object] = {"reset": True}
    if "batch_size" in constructor_parameters:
        spool_kwargs["batch_size"] = spool_batch_size

    started = perf_counter()
    cpu_started = process_time()
    rss_start = current_rss_bytes()
    rss_peak = rss_start
    temporary_peak = 0
    spool = RecordSpool(spool_path, **spool_kwargs)
    try:
        for offset in range(0, rows, source_batch_size):
            batch = [
                replace(record, symbol=symbol)
                for record in generate_records(
                    offset,
                    min(source_batch_size, rows - offset),
                )
            ]
            spool.append("acquired", batch)
            del batch
            rss_peak = max(rss_peak, current_rss_bytes())
            temporary_peak = max(temporary_peak, storage_bytes(spool_path))

        accepted: list[KlineRecord] = []

        def accept(record: KlineRecord) -> None:
            accepted.append(record)
            if len(accepted) >= 1_000:
                spool.append("validated", accepted)
                accepted.clear()

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
        validated_rows = spool.count("validated", DataType.KLINES, "5m")
        rss_peak = max(rss_peak, current_rss_bytes())
        temporary_peak = max(temporary_peak, storage_bytes(spool_path))

        digest = hashlib.sha256()
        reread_rows = 0
        previous = None
        for record in spool.iter_sorted("validated", DataType.KLINES, "5m"):
            if record.symbol != symbol:
                raise RuntimeError("multi-symbol spool leaked a foreign symbol")
            if previous is not None and record.open_time - previous != _STEP:
                raise RuntimeError("multi-symbol typed reread continuity changed")
            previous = record.open_time
            digest.update(
                (
                    f"{record.symbol}|{record.open_time.isoformat()}|{record.open}|"
                    f"{record.high}|{record.low}|{record.close}|{record.volume}\n"
                ).encode()
            )
            reread_rows += 1
        acquired_rows = spool.count("acquired", DataType.KLINES, "5m")
        physical_rows = int(spool._connection.execute("SELECT COUNT(*) FROM records").fetchone()[0])
        spool_bytes = storage_bytes(spool_path)
    finally:
        spool.close(remove=False)

    passed = (
        validation.status is ValidationStatus.VALID
        and not validation.issues
        and not validation.gaps
        and not validation.duplicates
        and acquired_rows == validated_rows == reread_rows == rows
    )
    if not passed:
        raise RuntimeError(
            f"multi-symbol gate failed for {symbol}: "
            f"status={validation.status.value}, acquired={acquired_rows}, "
            f"validated={validated_rows}, reread={reread_rows}"
        )
    return {
        "symbol": symbol,
        "rows": rows,
        "range": {
            "start": _START.isoformat(),
            "end_exclusive": (_START + rows * _STEP).isoformat(),
        },
        "validation": {
            "status": validation.status.value,
            "issues": len(validation.issues),
            "gaps": len(validation.gaps),
            "duplicates": len(validation.duplicates),
            "typed_reread_digest_sha256": digest.hexdigest(),
        },
        "spool": {
            "physical_record_rows": physical_rows,
            "serialized_payload_copies_per_logical_row": round(
                physical_rows / rows,
                6,
            ),
            "bytes_at_completion": spool_bytes,
            "peak_sampled_temporary_bytes": temporary_peak,
        },
        "performance": {
            "wall_seconds": round(perf_counter() - started, 6),
            "cpu_seconds": round(process_time() - cpu_started, 6),
        },
        "memory": {
            "rss_start_bytes": rss_start,
            "rss_peak_sampled_bytes": max(
                rss_peak,
                current_rss_bytes(),
                peak_rss_bytes(),
            ),
        },
        "gate": {"passed": True},
    }


def run(
    *,
    symbols: tuple[str, ...],
    rows_per_symbol: int,
    source_batch_size: int,
    spool_batch_size: int,
    workdir: Path,
    cache_condition: str,
) -> dict[str, Any]:
    started = perf_counter()
    cpu_started = process_time()
    io_start = process_io()
    results = [
        run_symbol(
            symbol=symbol,
            rows=rows_per_symbol,
            source_batch_size=source_batch_size,
            spool_batch_size=spool_batch_size,
            workdir=workdir,
        )
        for symbol in symbols
    ]
    io_end = process_io()
    return {
        "benchmark_version": 1,
        "benchmark_kind": "record_spool_multisymbol_validation_io",
        "git_sha": git_sha(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "cache_condition": cache_condition,
        "corpus": {
            "symbols": list(symbols),
            "timeframe": "5m",
            "rows_per_symbol": rows_per_symbol,
            "total_rows": rows_per_symbol * len(symbols),
            "source_batch_size": source_batch_size,
        },
        "symbols": results,
        "performance": {
            "wall_seconds": round(perf_counter() - started, 6),
            "cpu_seconds": round(process_time() - cpu_started, 6),
        },
        "memory": {
            "rss_peak_sampled_bytes": max(
                item["memory"]["rss_peak_sampled_bytes"] for item in results
            ),
        },
        "storage": {
            "peak_sampled_temporary_bytes_sum": sum(
                item["spool"]["peak_sampled_temporary_bytes"] for item in results
            ),
        },
        "io": {
            "process_counters_available": bool(io_start and io_end),
            "process_counter_delta": {
                name: max(0, io_end.get(name, 0) - value)
                for name, value in io_start.items()
                if name in io_end
            },
        },
        "gate": {
            "passed": all(item["gate"]["passed"] for item in results),
            "criteria": {
                "three_or_more_symbols": len(symbols) >= 3,
                "same_range_and_timeframe": True,
                "all_symbol_gates_passed": all(item["gate"]["passed"] for item in results),
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", nargs="+", default=list(_DEFAULT_SYMBOLS))
    parser.add_argument("--rows-per-symbol", type=int, default=8_928)
    parser.add_argument("--source-batch-size", type=int, default=8_192)
    parser.add_argument("--spool-batch-size", type=int, default=8_192)
    parser.add_argument("--cache-condition", default="warm-process-isolated")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workdir", type=Path)
    parser.add_argument("--keep-data", action="store_true")
    args = parser.parse_args()
    if len(set(args.symbols)) != len(args.symbols):
        raise SystemExit("--symbols must be unique")
    if len(args.symbols) < 3:
        raise SystemExit("--symbols requires at least three symbols")
    for name in ("rows_per_symbol", "source_batch_size", "spool_batch_size"):
        if getattr(args, name) <= 0:
            raise SystemExit(f"--{name.replace('_', '-')} must be positive")

    temporary = args.workdir is None
    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="mdi-multisymbol-"))
    workdir.mkdir(parents=True, exist_ok=True)
    try:
        result = run(
            symbols=tuple(args.symbols),
            rows_per_symbol=args.rows_per_symbol,
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
