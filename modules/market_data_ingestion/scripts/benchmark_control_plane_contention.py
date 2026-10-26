from __future__ import annotations

import argparse
import asyncio
import gc
import json
import sqlite3
import tempfile
import threading
import time
import warnings
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from market_data_application import (
    ApplicationSettings,
    CreateDownloadRequest,
    MarketDataApplication,
    build_application_context,
)
from market_data_application.contracts import StorageFormat
from market_data_ingestion.adapters import FakeExchangeAdapter
from market_data_ingestion.enums import DataType, MarketType


def _request(ordinal: int) -> CreateDownloadRequest:
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=(ordinal % 10_000) * 5)
    return CreateDownloadRequest(
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=start,
        end=start + timedelta(minutes=10),
        storage_format=StorageFormat.CSV,
        concurrency=1,
        max_records_in_memory=10_000,
    )


def _summary(samples: list[int]) -> dict[str, int | float]:
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def percentile(value: float) -> float:
        index = max(0, min(len(ordered) - 1, int(len(ordered) * value + 0.999) - 1))
        return ordered[index] / 1_000_000

    return {
        "count": len(ordered),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
    }


def _sqlite_fd_count() -> int | None:
    for fd_root in (Path("/proc/self/fd"), Path("/dev/fd")):
        if not fd_root.is_dir():
            continue
        count = 0
        for entry in fd_root.iterdir():
            try:
                target = entry.readlink().as_posix()
            except OSError:
                continue
            if ".sqlite3" in target:
                count += 1
        return count
    return None


def _async_task_count() -> int:
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return 0
    return len(asyncio.all_tasks(loop))


def _application(root: Path) -> MarketDataApplication:
    return MarketDataApplication(
        build_application_context(
            ApplicationSettings(
                storage_root=root,
                instance_id="wave2e-contention-benchmark",
                worker_count=2,
            ),
            adapters={"fake": FakeExchangeAdapter(chunk_bars=6)},
        )
    )


def run_contention_probe(root: Path, *, attempts: int = 100) -> dict[str, Any]:
    if attempts < 10:
        raise ValueError("attempts must be at least 10")
    root = root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    application = _application(root)
    sqlite_before = _sqlite_fd_count()
    threads_before = threading.active_count()
    tasks_before = _async_task_count()
    latencies: list[int] = []
    busy_errors = 0
    unexpected_errors = 0

    def operation(index: int) -> int:
        started = time.perf_counter_ns()
        if index % 5 == 0:
            application.create_download(
                _request(index),
                idempotency_key=f"wave2e-contention-{index:05d}",
                correlation_id="corr-contention",
                actor_id="wave2e-benchmark",
            )
        else:
            application.list_jobs(limit=50)
        return time.perf_counter_ns() - started

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always", ResourceWarning)
        try:
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(operation, index) for index in range(attempts)]
                for future in futures:
                    try:
                        latencies.append(future.result())
                    except sqlite3.OperationalError as error:
                        is_busy = "locked" in str(error).lower() or "busy" in str(error).lower()
                        if is_busy:
                            busy_errors += 1
                        else:
                            unexpected_errors += 1
                    except Exception:
                        unexpected_errors += 1

            baseline_ns = latencies[len(latencies) // 2] if latencies else 0
            lock_connection = sqlite3.connect(application.context.settings.jobs_path, timeout=10)
            lock_connection.execute("PRAGMA journal_mode=WAL")
            lock_connection.execute("BEGIN IMMEDIATE")
            started = threading.Event()

            def blocked_writer() -> int:
                started.set()
                before = time.perf_counter_ns()
                application.create_download(
                    _request(attempts + 1_000),
                    idempotency_key="wave2e-lock-probe",
                    correlation_id="corr-lock-probe",
                    actor_id="wave2e-benchmark",
                )
                return time.perf_counter_ns() - before

            try:
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(blocked_writer)
                    if not started.wait(timeout=1):
                        raise AssertionError("lock-probe writer did not start")
                    hold_ms = 50.0
                    time.sleep(hold_ms / 1_000)
                    lock_connection.rollback()
                    blocked_ns = future.result(timeout=5)
            finally:
                lock_connection.close()
        finally:
            application.shutdown()
        gc.collect()

    sqlite_after = _sqlite_fd_count()
    threads_after = threading.active_count()
    tasks_after = _async_task_count()
    warning_count = sum(issubclass(item.category, ResourceWarning) for item in captured)
    observed_ms = blocked_ns / 1_000_000
    return {
        "schema_version": 1,
        "environment": {
            "no_network": True,
            "worker_threads": 8,
            "attempts": attempts,
            "writer_attempts": (attempts + 4) // 5,
            "reader_attempts": attempts - (attempts + 4) // 5,
        },
        "latency": _summary(latencies),
        "availability": len(latencies) / attempts,
        "busy_error_count": busy_errors,
        "unexpected_error_count": unexpected_errors,
        "lock_probe": {
            "configured_hold_ms": hold_ms,
            "observed_writer_latency_ms": observed_ms,
            "estimated_lock_wait_ms": max(0.0, observed_ms - baseline_ns / 1_000_000),
            "completed_without_busy_error": True,
        },
        "resources": {
            "sqlite_fd_before": sqlite_before,
            "sqlite_fd_after": sqlite_after,
            "sqlite_connection_count_after": sqlite_after,
            "thread_growth": threads_after - threads_before,
            "task_growth": tasks_after - tasks_before,
            "resource_warning_count": warning_count,
        },
        "verdict": {
            "availability_full": len(latencies) == attempts,
            "busy_errors_zero": busy_errors == 0,
            "unexpected_errors_zero": unexpected_errors == 0,
            "sqlite_connections_closed": sqlite_after in {None, 0},
            "thread_stable": threads_after <= threads_before + 1,
            "task_stable": tasks_after == tasks_before,
            "resource_warnings_zero": warning_count == 0,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(prog="benchmark-control-plane-contention")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--attempts", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.root is None:
        with tempfile.TemporaryDirectory(prefix="wave2e-contention-") as directory:
            result = run_contention_probe(Path(directory), attempts=args.attempts)
    else:
        result = run_contention_probe(args.root, attempts=args.attempts)
    serialized = json.dumps(result, indent=2, sort_keys=True)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
