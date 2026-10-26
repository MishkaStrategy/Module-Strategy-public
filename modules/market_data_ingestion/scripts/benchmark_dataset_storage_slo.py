from __future__ import annotations

import argparse
import hashlib
import json
import platform
import random
import resource
import sqlite3
import tempfile
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from market_data_ingestion.contracts import DatasetDescriptor
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from market_data_ingestion.errors import RegistryError, StorageError
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage

BENCHMARK_VERSION = 1
DEFAULT_SEED = 20260802


def _summary(samples: list[int]) -> dict[str, int | float]:
    ordered = sorted(samples)
    if not ordered:
        return {"count": 0, "p50_ms": 0.0, "p95_ms": 0.0, "p99_ms": 0.0}

    def value(percentile: float) -> float:
        index = max(
            0,
            min(len(ordered) - 1, int(len(ordered) * percentile + 0.999) - 1),
        )
        return ordered[index] / 1_000_000

    return {
        "count": len(ordered),
        "p50_ms": value(0.50),
        "p95_ms": value(0.95),
        "p99_ms": value(0.99),
    }


def _timed(operation: Callable[[], Any]) -> tuple[int, Any]:
    started = time.perf_counter_ns()
    result = operation()
    return time.perf_counter_ns() - started, result


def _rss_mib() -> float:
    rss = float(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return rss / (1024 * 1024) if platform.system() == "Darwin" else rss / 1024


def _fd_count() -> int | None:
    for path in (Path("/proc/self/fd"), Path("/dev/fd")):
        try:
            return len(tuple(path.iterdir()))
        except OSError:
            continue
    return None


def _descriptor(root: Path, ordinal: int) -> DatasetDescriptor:
    dataset_id = f"bench-dataset-{ordinal:05d}"
    start = datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=ordinal * 5)
    return DatasetDescriptor(
        dataset_id=dataset_id,
        version=1,
        dataset_hash=hashlib.sha256(dataset_id.encode()).hexdigest(),
        manifest_uri=f"manifests/{dataset_id}/v1.json",
        storage_uri=root.as_uri(),
        exchange_id="benchmark",
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument_id=f"benchmark:usdt-m-perpetual:s{ordinal:05d}",
        data_types=(DataType.KLINES,),
        timeframes=("5m",),
        start=start,
        end=start + timedelta(minutes=5),
        schema_version=1,
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        lineage=(f"synthetic:{ordinal:05d}",),
    )


def _sizes(database: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    suffixes = (("database_bytes", ""), ("wal_bytes", "-wal"), ("shm_bytes", "-shm"))
    for name, suffix in suffixes:
        path = database.with_name(database.name + suffix)
        result[name] = path.stat().st_size if path.exists() else 0
    return result


def _plans(database: Path, descriptor: DatasetDescriptor) -> dict[str, list[str]]:
    statements = {
        "exact": (
            "SELECT descriptor_json FROM datasets WHERE dataset_id=? AND version=?",
            (descriptor.dataset_id, 1),
        ),
        "latest": (
            "SELECT descriptor_json FROM datasets WHERE dataset_id=? ORDER BY version DESC LIMIT 1",
            (descriptor.dataset_id,),
        ),
        "checksum": (
            "SELECT descriptor_json FROM datasets WHERE dataset_hash=?",
            (descriptor.dataset_hash,),
        ),
    }
    with closing(sqlite3.connect(database)) as connection:
        return {
            name: [
                str(row[3]) for row in connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters)
            ]
            for name, (sql, parameters) in statements.items()
        }


def _concurrency(
    registry: SQLiteDatasetRegistry,
    root: Path,
    descriptors: list[DatasetDescriptor],
    seed: int,
) -> dict[str, Any]:
    def reader(worker: int) -> tuple[int, int]:
        rng = random.Random(seed + worker)
        successes = 0
        busy = 0
        for _ in range(100):
            item = descriptors[rng.randrange(len(descriptors))]
            try:
                successes += registry.inspect(item.dataset_id, 1) is not None
            except sqlite3.OperationalError as exc:
                busy += "locked" in str(exc).lower()
        return successes, busy

    def writer(worker: int) -> tuple[list[int], int]:
        samples: list[int] = []
        busy = 0
        for offset in range(25):
            item = _descriptor(root, len(descriptors) + 1 + worker * 25 + offset)
            try:
                elapsed, _ = _timed(lambda item=item: registry.register(item))
                samples.append(elapsed)
            except sqlite3.OperationalError as exc:
                busy += "locked" in str(exc).lower()
        return samples, busy

    with ThreadPoolExecutor(max_workers=8) as executor:
        readers = [executor.submit(reader, worker) for worker in range(4)]
        writers = [executor.submit(writer, worker) for worker in range(4)]
    reader_results = [future.result() for future in readers]
    writer_results = [future.result() for future in writers]
    writer_samples = [sample for samples, _ in writer_results for sample in samples]
    reader_successes = sum(successes for successes, _ in reader_results)
    busy_errors = sum(busy for _, busy in reader_results + writer_results)
    return {
        "reader_attempts": 400,
        "reader_successes": reader_successes,
        "reader_availability": reader_successes / 400,
        "writer_attempts": 100,
        "writer_successes": len(writer_samples),
        "busy_error_count": busy_errors,
        "transaction_retry_count": 0,
        "writer_latency": _summary(writer_samples),
    }


def run_registry_workload(root: Path, count: int, *, seed: int) -> dict[str, Any]:
    database = root / "registry.sqlite3"
    registry = SQLiteDatasetRegistry(database)
    keeper = sqlite3.connect(database)
    keeper.execute("PRAGMA journal_mode=WAL")
    keeper.execute("BEGIN")
    keeper.execute("SELECT COUNT(*) FROM datasets").fetchone()

    descriptors = [_descriptor(root, index) for index in range(count)]
    register_samples: list[int] = []
    started = time.perf_counter()
    for descriptor in descriptors:
        elapsed, _ = _timed(lambda descriptor=descriptor: registry.register(descriptor))
        register_samples.append(elapsed)
    registration_seconds = time.perf_counter() - started

    duplicate_samples = [
        _timed(lambda descriptor=descriptor: registry.register(descriptor))[0]
        for descriptor in descriptors[: min(100, count)]
    ]
    conflict_classified = False
    try:
        registry.register(
            replace(
                descriptors[0],
                dataset_hash=hashlib.sha256(b"conflict").hexdigest(),
            )
        )
    except RegistryError as exc:
        conflict_classified = exc.code == "REGISTRATION_CONFLICT"

    rng = random.Random(seed + count)
    selected = [descriptors[rng.randrange(count)] for _ in range(min(1000, count))]
    exact_samples: list[int] = []
    latest_samples: list[int] = []
    for descriptor in selected:
        exact, found = _timed(
            lambda descriptor=descriptor: registry.inspect(descriptor.dataset_id, 1)
        )
        latest, newest = _timed(
            lambda descriptor=descriptor: registry.inspect(descriptor.dataset_id)
        )
        if found is None or newest is None:
            raise AssertionError("committed descriptor was not readable")
        exact_samples.append(exact)
        latest_samples.append(latest)

    checksum_samples: list[int] = []
    with closing(sqlite3.connect(database)) as connection:
        for descriptor in selected:
            elapsed, row = _timed(
                lambda descriptor=descriptor: connection.execute(
                    "SELECT descriptor_json FROM datasets WHERE dataset_hash=?",
                    (descriptor.dataset_hash,),
                ).fetchone()
            )
            if row is None:
                raise AssertionError("checksum lookup lost a committed descriptor")
            checksum_samples.append(elapsed)

    concurrency = _concurrency(registry, root, descriptors, seed + count)
    with closing(sqlite3.connect(database)) as connection:
        before = int(connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0])
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO datasets VALUES(?,?,?,?,?)",
            (
                "rollback-probe",
                1,
                hashlib.sha256(b"rollback").hexdigest(),
                "{}",
                datetime.now(UTC).isoformat(),
            ),
        )
        connection.rollback()
        after = int(connection.execute("SELECT COUNT(*) FROM datasets").fetchone()[0])

    sizes = _sizes(database)
    keeper.commit()
    keeper.close()
    reopen_ns, reopened = _timed(lambda: SQLiteDatasetRegistry(database))
    if reopened.inspect(descriptors[-1].dataset_id, 1) is None:
        raise AssertionError("reopen lost a committed descriptor")
    with closing(sqlite3.connect(database)) as connection:
        integrity_ns, integrity = _timed(
            lambda: str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        )
        user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

    return {
        "dataset_count": count,
        "final_dataset_count": after,
        "registrations_per_second": count / registration_seconds,
        "register_latency": _summary(register_samples),
        "duplicate_register_latency": _summary(duplicate_samples),
        "exact_lookup_latency": _summary(exact_samples),
        "latest_lookup_latency": _summary(latest_samples),
        "checksum_lookup_latency": _summary(checksum_samples),
        "query_plans": _plans(database, descriptors[0]),
        "concurrency": concurrency,
        "conflicting_registration_classified": conflict_classified,
        "transaction_rollback_preserved_count": before == after,
        "reopen_ms": reopen_ns / 1_000_000,
        "integrity_check_ms": integrity_ns / 1_000_000,
        "integrity_check_result": integrity,
        "pragma_user_version": user_version,
        "sizes_with_reader_snapshot": sizes,
        "bytes_per_dataset": sum(sizes.values()) / after,
    }


def _temp_inventory(root: Path) -> tuple[list[str], int]:
    files = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob(".*.tmp")
        if path.is_file() and not path.is_symlink()
    )
    return files, sum((root / item).stat().st_size for item in files)


def run_local_storage_workload(root: Path, *, manifest_count: int) -> dict[str, Any]:
    storage = LocalStorage(root)
    publish_samples: list[int] = []
    for index in range(manifest_count):
        payload = {
            "dataset_id": f"storage-bench-{index:05d}",
            "partitions": [
                hashlib.sha256(f"{index}:{part}".encode()).hexdigest() for part in range(8)
            ],
        }
        elapsed, _ = _timed(
            lambda index=index, payload=payload: storage.write_manifest(
                f"storage-bench-{index:05d}", 1, payload
            )
        )
        publish_samples.append(elapsed)

    large_payload = {
        "dataset_id": "storage-bench-large",
        "partitions": [
            {
                "id": f"part:{index:05d}",
                "uri": f"normalized/benchmark/part-{index:05d}.parquet",
                "sha256": hashlib.sha256(f"large:{index}".encode()).hexdigest(),
            }
            for index in range(10_000)
        ],
    }
    publish_ns, large_path = _timed(
        lambda: storage.write_manifest("storage-bench-large", 1, large_payload)
    )
    idempotent_ns, _ = _timed(
        lambda: storage.write_manifest("storage-bench-large", 1, large_payload)
    )
    original = large_path.read_bytes()
    large_path.write_bytes(b"corrupted")
    detected = False
    started = time.perf_counter_ns()
    try:
        storage.write_manifest("storage-bench-large", 1, large_payload)
    except StorageError as exc:
        detected = exc.code == "IMMUTABLE_CONFLICT"
    detection_ns = time.perf_counter_ns() - started
    large_path.write_bytes(original)

    orphan = large_path.parent / ".crash-orphan.tmp"
    orphan.write_bytes(b"orphan")
    before_paths, before_bytes = _temp_inventory(root)
    cleanup_supported = hasattr(storage, "cleanup_orphan_temporary_files")
    cleaned: list[str] = []
    if cleanup_supported:
        cleaned = [
            path.relative_to(root).as_posix()
            for path in storage.cleanup_orphan_temporary_files(max_age_seconds=0)
        ]
    after_paths, after_bytes = _temp_inventory(root)

    list_ns, listed = _timed(
        lambda: sorted(
            path.relative_to(root).as_posix()
            for path in (root / "manifests").rglob("*.json")
            if path.is_file() and not path.is_symlink()
        )
    )
    stale = root / "locks" / "stale-benchmark.lock"
    stale.write_text("pid=999999999\ncreated_at=2020-01-01T00:00:00+00:00\n")
    stale_ns, _ = _timed(lambda: _exercise_lock(storage))
    reopen_ns, reopened = _timed(lambda: LocalStorage(root))
    if reopened.root != storage.root:
        raise AssertionError("LocalStorage reopen changed root")
    tree_bytes = sum(
        path.stat().st_size for path in root.rglob("*") if path.is_file() and not path.is_symlink()
    )
    return {
        "manifest_count": manifest_count,
        "small_manifest_publish_latency": _summary(publish_samples),
        "large_manifest_partition_count": 10_000,
        "large_manifest_bytes": large_path.stat().st_size,
        "large_manifest_publish_ms": publish_ns / 1_000_000,
        "idempotent_manifest_publish_ms": idempotent_ns / 1_000_000,
        "immutable_conflict_detected": detected,
        "corruption_detection_ms": detection_ns / 1_000_000,
        "deterministic_list_count": len(listed),
        "deterministic_list_ms": list_ns / 1_000_000,
        "cleanup_api_supported": cleanup_supported,
        "orphan_paths_before": before_paths,
        "orphan_paths_after": after_paths,
        "cleaned_paths": cleaned,
        "temp_bytes_before": before_bytes,
        "temp_bytes_after": after_bytes,
        "stale_lock_recovery_ms": stale_ns / 1_000_000,
        "reopen_ms": reopen_ns / 1_000_000,
        "tree_bytes": tree_bytes,
    }


def _exercise_lock(storage: LocalStorage) -> None:
    with storage.lock("stale-benchmark"):
        pass


def run_benchmark(
    *,
    dataset_counts: tuple[int, ...],
    manifest_count: int,
    seed: int = DEFAULT_SEED,
) -> dict[str, Any]:
    if not dataset_counts or any(count <= 0 for count in dataset_counts):
        raise ValueError("dataset_counts must contain positive integers")
    if manifest_count <= 0:
        raise ValueError("manifest_count must be positive")
    fd_before = _fd_count()
    rss_before = _rss_mib()
    started_at = datetime.now(UTC)
    with tempfile.TemporaryDirectory(prefix="dataset-storage-slo-") as temporary:
        root = Path(temporary).resolve()
        registry = [
            run_registry_workload(root / f"registry-{count}", count, seed=seed)
            for count in dataset_counts
        ]
        local_storage = run_local_storage_workload(
            root / "local-storage", manifest_count=manifest_count
        )
    fd_after = _fd_count()
    rss_after = _rss_mib()
    return {
        "benchmark_version": BENCHMARK_VERSION,
        "started_at": started_at.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "sqlite": sqlite3.sqlite_version,
            "seed": seed,
        },
        "process": {
            "peak_rss_growth_mib": max(0.0, rss_after - rss_before),
            "fd_growth": (
                fd_after - fd_before if fd_before is not None and fd_after is not None else None
            ),
        },
        "registry": registry,
        "local_storage": local_storage,
        "unsupported_baseline_contracts": [
            "symbol/timeframe lookup is not exposed by SQLiteDatasetRegistry",
            "lineage traversal is validated in manifests, not queried by the registry",
            "registry schema downgrade rejection is not implemented",
            "LocalStorage has no public deterministic manifest listing contract",
            "LocalStorage has no public crash-orphan temporary cleanup contract",
        ],
    }


def _parse_counts(value: str) -> tuple[int, ...]:
    counts = tuple(int(item) for item in value.split(",") if item)
    if not counts or any(count <= 0 for count in counts):
        raise argparse.ArgumentTypeError("dataset counts must be positive")
    return counts


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--dataset-counts",
        type=_parse_counts,
        default=(1, 100, 1000, 10000),
    )
    parser.add_argument("--manifest-count", type=int, default=100)
    parser.add_argument("--seed", type=int, default=DEFAULT_SEED)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_benchmark(
        dataset_counts=args.dataset_counts,
        manifest_count=args.manifest_count,
        seed=args.seed,
    )
    payload = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
