from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.registry_schema import VersionedSQLiteDatasetRegistry
from market_data_ingestion.storage_recovery import RecoverableLocalStorage
from scripts.benchmark_dataset_storage_slo import _descriptor

RECOVERY_BENCHMARK_VERSION = 1


def _timed(operation: Any) -> tuple[int, Any]:
    started = time.perf_counter_ns()
    result = operation()
    return time.perf_counter_ns() - started, result


def _temporary_bytes(root: Path) -> int:
    return sum(
        path.stat().st_size
        for path in root.rglob(".*.tmp")
        if path.is_file() and not path.is_symlink()
    )


def run_recovery_benchmark(
    *,
    manifest_count: int = 100,
    orphan_count: int = 100,
) -> dict[str, Any]:
    if manifest_count <= 0 or orphan_count <= 0:
        raise ValueError("manifest_count and orphan_count must be positive")
    with tempfile.TemporaryDirectory(prefix="dataset-storage-recovery-") as temporary:
        root = Path(temporary).resolve()
        registry_path = root / "registry.sqlite3"
        legacy = SQLiteDatasetRegistry(registry_path)
        legacy.register(_descriptor(root, 1))
        adoption_ns, registry = _timed(lambda: VersionedSQLiteDatasetRegistry(registry_path))
        if registry.inspect("bench-dataset-00001", 1) is None:
            raise AssertionError("schema adoption lost a committed descriptor")

        crash_code = f"""
import os
from pathlib import Path
from market_data_ingestion.registry_schema import VersionedSQLiteDatasetRegistry
from scripts.benchmark_dataset_storage_slo import _descriptor
root = Path({str(root)!r})
registry = VersionedSQLiteDatasetRegistry(root / 'registry.sqlite3')
registry.register(_descriptor(root, 2))
os._exit(0)
"""
        subprocess.run([sys.executable, "-c", crash_code], check=True)
        crash_recovery_ns, recovered = _timed(lambda: VersionedSQLiteDatasetRegistry(registry_path))
        if recovered.inspect("bench-dataset-00002", 1) is None:
            raise AssertionError("crash recovery lost a committed descriptor")
        with closing(sqlite3.connect(registry_path)) as connection:
            integrity_ns, integrity = _timed(
                lambda: str(connection.execute("PRAGMA integrity_check").fetchone()[0])
            )
            user_version = int(connection.execute("PRAGMA user_version").fetchone()[0])

        storage = RecoverableLocalStorage(root / "storage")
        for index in range(manifest_count):
            storage.write_manifest(
                f"recovery-manifest-{index:05d}",
                1,
                {"dataset_id": f"recovery-manifest-{index:05d}", "version": 1},
            )
        list_ns, manifests = _timed(storage.list_manifests)
        orphan_root = storage.root / "manifests" / "recovery-manifest-00000"
        for index in range(orphan_count):
            orphan = orphan_root / f".v1.json.crash-{index:05d}.tmp"
            orphan.write_bytes(f"orphan-{index}".encode())
            os.utime(orphan, (0, 0))
        temporary_before = _temporary_bytes(storage.root)
        cleanup_ns, removed = _timed(
            lambda: storage.cleanup_orphan_temporary_files(max_age_seconds=60)
        )
        temporary_after = _temporary_bytes(storage.root)
        reopen_ns, reopened = _timed(lambda: RecoverableLocalStorage(storage.root))
        if reopened.list_manifests() != manifests:
            raise AssertionError("storage reopen changed deterministic manifest inventory")

        return {
            "recovery_benchmark_version": RECOVERY_BENCHMARK_VERSION,
            "environment": {
                "python": sys.version.split()[0],
                "sqlite": sqlite3.sqlite_version,
            },
            "registry": {
                "schema_adoption_ms": adoption_ns / 1_000_000,
                "crash_recovery_ms": crash_recovery_ns / 1_000_000,
                "integrity_check_ms": integrity_ns / 1_000_000,
                "integrity_check_result": integrity,
                "schema_version": user_version,
            },
            "local_storage": {
                "manifest_count": len(manifests),
                "deterministic_list_ms": list_ns / 1_000_000,
                "orphan_count": orphan_count,
                "orphan_cleanup_ms": cleanup_ns / 1_000_000,
                "orphan_cleanup_files_per_second": (
                    len(removed) / (cleanup_ns / 1_000_000_000) if cleanup_ns else 0.0
                ),
                "removed_orphan_count": len(removed),
                "temporary_bytes_before": temporary_before,
                "temporary_bytes_after": temporary_after,
                "reopen_ms": reopen_ns / 1_000_000,
            },
            "completed_at": datetime.now(UTC).isoformat(),
        }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--manifest-count", type=int, default=100)
    parser.add_argument("--orphan-count", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = run_recovery_benchmark(
        manifest_count=args.manifest_count,
        orphan_count=args.orphan_count,
    )
    payload = json.dumps(result, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
