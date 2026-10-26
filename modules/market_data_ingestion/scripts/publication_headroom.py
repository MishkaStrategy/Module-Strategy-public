from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from market_data_ingestion.canonical import primitive, sha256_file
from market_data_ingestion.contracts import DatasetDescriptor, InstrumentReference
from market_data_ingestion.enums import DataType, MarketType, ValidationStatus
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage

_TIMEFRAME = "5m"
_INTERVAL = timedelta(minutes=5)
_START = datetime(2019, 1, 1, tzinfo=UTC)
_SOURCE_ID = "raw:synthetic-benchmark"


@dataclass(frozen=True, slots=True)
class PublicationMeasurement:
    rows: int
    partition_count: int
    final_storage_bytes: int
    peak_storage_bytes: int
    largest_atomic_payload_bytes: int
    largest_partition_bytes: int
    manifest_bytes: int
    registry_bytes_before: int
    registry_bytes_after: int
    registry_growth_bytes: int
    registry_integrity: str
    descriptor_reopen_match: bool

    def to_dict(self) -> dict[str, object]:
        return primitive(self)


def _tree_bytes(root: Path) -> int:
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


def _registry_bytes(database: Path) -> int:
    return sum(
        path.stat().st_size
        for path in (
            database,
            Path(f"{database}-wal"),
            Path(f"{database}-shm"),
        )
        if path.is_file()
    )


def _registry_integrity(database: Path) -> str:
    connection = sqlite3.connect(database)
    try:
        row = connection.execute("PRAGMA integrity_check").fetchone()
    finally:
        connection.close()
    return str(row[0]) if row is not None else "missing"


def _record(timestamp: datetime) -> KlineRecord:
    return KlineRecord(
        open_time=timestamp,
        close_time=timestamp + _INTERVAL,
        open="100",
        high="101",
        low="99",
        close="100.5",
        volume="1",
        quote_volume="100.5",
        trade_count=1,
        taker_buy_base_volume="0.5",
        taker_buy_quote_volume="50.25",
        symbol="BTCUSDT",
        timeframe=_TIMEFRAME,
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL.value,
        source_kind="synthetic_benchmark",
    )


def _monthly_batches(rows: int) -> Iterator[tuple[KlineRecord, ...]]:
    if rows <= 0:
        raise ValueError("rows must be positive")
    remaining = rows
    cursor = _START
    while remaining:
        month = (cursor.year, cursor.month)
        batch: list[KlineRecord] = []
        while remaining and (cursor.year, cursor.month) == month:
            batch.append(_record(cursor))
            cursor += _INTERVAL
            remaining -= 1
        yield tuple(batch)


@contextmanager
def _measure_atomic_replaces(storage: LocalStorage) -> Iterator[dict[str, int]]:
    real_replace = os.replace
    metrics = {"peak_storage_bytes": 0, "largest_atomic_payload_bytes": 0}

    def measured_replace(source: os.PathLike[str] | str, target: os.PathLike[str] | str) -> None:
        source_path = Path(source)
        if source_path.is_file():
            metrics["largest_atomic_payload_bytes"] = max(
                metrics["largest_atomic_payload_bytes"], source_path.stat().st_size
            )
        metrics["peak_storage_bytes"] = max(
            metrics["peak_storage_bytes"], _tree_bytes(storage.root)
        )
        real_replace(source, target)

    with patch("market_data_ingestion.storage.os.replace", measured_replace):
        yield metrics


def measure_publication(*, rows: int, root: Path) -> PublicationMeasurement:
    storage = LocalStorage(root / "storage")
    registry_path = root / "registry" / "datasets.sqlite3"
    registry = SQLiteDatasetRegistry(registry_path)
    registry_before = _registry_bytes(registry_path)
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt-m:btcusdt",
    )

    partitions = []
    largest_partition_bytes = 0
    with _measure_atomic_replaces(storage) as atomic_metrics:
        for batch in _monthly_batches(rows):
            partition = storage.write_kline_partition(
                dataset_id="bench-btcusdt-5m",
                version=1,
                layer="normalized",
                instrument=instrument,
                timeframe=_TIMEFRAME,
                records=batch,
                source_artifact_ids=(_SOURCE_ID,),
                format_name="parquet",
                compression="zstd",
            )
            partitions.append(partition)
            partition_path = storage.root / partition.storage_uri
            largest_partition_bytes = max(largest_partition_bytes, partition_path.stat().st_size)

        manifest_payload = {
            "dataset_id": "bench-btcusdt-5m",
            "version": 1,
            "synthetic_only": True,
            "timeframe": _TIMEFRAME,
            "rows": rows,
            "partitions": [primitive(item) for item in partitions],
            "source_artifact_ids": [_SOURCE_ID],
        }
        manifest_path = storage.write_manifest("bench-btcusdt-5m", 1, manifest_payload)
        manifest_bytes = manifest_path.stat().st_size

    if not partitions:
        raise RuntimeError("benchmark produced no partitions")
    dataset_hash = sha256_file(manifest_path)
    descriptor = DatasetDescriptor(
        dataset_id="bench-btcusdt-5m",
        version=1,
        dataset_hash=dataset_hash,
        manifest_uri=manifest_path.relative_to(storage.root).as_posix(),
        storage_uri="file:///synthetic/market-data",
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument_id=instrument.canonical_id,
        data_types=(DataType.KLINES,),
        timeframes=(_TIMEFRAME,),
        start=partitions[0].start,
        end=partitions[-1].end,
        schema_version=1,
        validation_status=ValidationStatus.VALID,
        unresolved_issues=(),
        lineage=(_SOURCE_ID,),
    )
    registry.register(descriptor)
    registry_after = _registry_bytes(registry_path)

    reopened = SQLiteDatasetRegistry(registry_path)
    observed = reopened.inspect(descriptor.dataset_id, descriptor.version)
    descriptor_match = observed == primitive(descriptor)
    integrity = _registry_integrity(registry_path)

    final_storage_bytes = _tree_bytes(storage.root)
    return PublicationMeasurement(
        rows=rows,
        partition_count=len(partitions),
        final_storage_bytes=final_storage_bytes,
        peak_storage_bytes=max(atomic_metrics["peak_storage_bytes"], final_storage_bytes),
        largest_atomic_payload_bytes=atomic_metrics["largest_atomic_payload_bytes"],
        largest_partition_bytes=largest_partition_bytes,
        manifest_bytes=manifest_bytes,
        registry_bytes_before=registry_before,
        registry_bytes_after=registry_after,
        registry_growth_bytes=registry_after - registry_before,
        registry_integrity=integrity,
        descriptor_reopen_match=descriptor_match,
    )


def _digest(measurement: PublicationMeasurement) -> str:
    payload = json.dumps(measurement.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--rows",
        type=int,
        action="append",
        default=None,
        help="Synthetic 5m row count. Repeat the option for multiple scales.",
    )
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    scales = tuple(args.rows or (10_000, 100_000, 721_597))
    if args.repeats <= 0:
        parser.error("--repeats must be positive")
    if any(rows <= 0 for rows in scales):
        parser.error("--rows values must be positive")

    measurements: list[dict[str, object]] = []
    for rows in scales:
        for repeat in range(args.repeats):
            with tempfile.TemporaryDirectory(prefix="market-data-headroom-") as temporary:
                measurement = measure_publication(rows=rows, root=Path(temporary))
            measurements.append(
                {
                    "rows": rows,
                    "repeat": repeat + 1,
                    "measurement": measurement.to_dict(),
                    "measurement_sha256": _digest(measurement),
                }
            )

    print(
        json.dumps(
            {
                "schema_version": 1,
                "synthetic_only": True,
                "timeframe": _TIMEFRAME,
                "scales": list(scales),
                "repeats": args.repeats,
                "measurements": measurements,
                "physical_watermark_adopted": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
