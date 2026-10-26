from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .canonical import sha256_file
from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    NormalizedPartitionDescriptor,
)
from .enums import DataType, ValidationStatus
from .errors import RegistryError, StorageError
from .integrity import RegisteredPathError, resolve_registered_path, verify_registered_dataset
from .manifest_io import load_manifest
from .records import KlineRecord
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .timeframes import timeframe_delta, timeframe_seconds

_BATCH_SIZE = 65_536
_KLINE_COLUMNS = (
    "open_time",
    "close_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "quote_volume",
    "trade_count",
    "taker_buy_base_volume",
    "taker_buy_quote_volume",
    "symbol",
    "timeframe",
    "exchange_id",
    "market_type",
    "source_kind",
    "is_closed",
)


@dataclass(frozen=True, slots=True)
class RegisteredKlineSelection:
    storage_root: Path
    descriptor: DatasetDescriptor
    manifest: DatasetManifest
    timeframe: str
    partitions: tuple[NormalizedPartitionDescriptor, ...]

    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.partitions)

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset_id": self.descriptor.dataset_id,
            "version": self.descriptor.version,
            "dataset_hash": self.descriptor.dataset_hash,
            "instrument_id": self.descriptor.instrument_id,
            "symbol": self.manifest.instrument.symbol,
            "timeframe": self.timeframe,
            "partition_count": len(self.partitions),
            "row_count": self.row_count,
            "start": self.partitions[0].start.isoformat(),
            "end": self.partitions[-1].end.isoformat(),
            "validation_status": self.descriptor.validation_status.value,
        }


def _validated_storage_root(value: Path) -> Path:
    requested = value.expanduser()
    if requested.is_symlink():
        raise StorageError(
            "DATASET_STORAGE_SYMLINK",
            "registered dataset storage root must not be a symbolic link",
            False,
            "dataset_read",
        )
    try:
        root = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise StorageError(
            "DATASET_STORAGE_MISSING",
            "registered dataset storage root does not exist",
            False,
            "dataset_read",
        ) from exc
    if not root.is_dir():
        raise StorageError(
            "DATASET_STORAGE_INVALID",
            "registered dataset storage root must be a directory",
            False,
            "dataset_read",
        )
    return root


def _utc_boundary(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def resolve_registered_kline_dataset(
    storage_root: Path,
    dataset_id: str,
    version: int,
    *,
    timeframe: str = "5m",
    consumer: str = "modular_backtest",
    deep: bool = True,
) -> RegisteredKlineSelection:
    root = _validated_storage_root(storage_root)
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError("dataset version must be a positive integer")
    normalized_timeframe = timeframe.strip().lower()
    timeframe_seconds(normalized_timeframe)

    registry_path = root / "registry.sqlite3"
    if registry_path.is_symlink() or not registry_path.is_file():
        raise RegistryError(
            "DATASET_REGISTRY_MISSING",
            "registered dataset registry is missing or unsafe",
            False,
            "dataset_read",
            {"registry_path": str(registry_path)},
        )
    descriptor_payload = SQLiteDatasetRegistry(registry_path).inspect(dataset_id, version)
    if descriptor_payload is None:
        raise RegistryError(
            "DATASET_NOT_FOUND",
            "requested dataset identity is not registered",
            False,
            "dataset_read",
            {"dataset_id": dataset_id, "version": version},
        )
    descriptor = descriptor_from_dict(descriptor_payload)
    if descriptor.version != version or descriptor.dataset_id != dataset_id:
        raise RegistryError(
            "DATASET_IDENTITY_MISMATCH",
            "registry returned a descriptor with a different identity",
            False,
            "dataset_read",
        )
    if descriptor.storage_uri != root.as_uri():
        raise RegistryError(
            "DATASET_STORAGE_IDENTITY_MISMATCH",
            "descriptor storage URI differs from the configured storage root",
            False,
            "dataset_read",
            {"expected": root.as_uri(), "actual": descriptor.storage_uri},
        )
    if descriptor.validation_status is not ValidationStatus.VALID or descriptor.unresolved_issues:
        raise RegistryError(
            "DATASET_NOT_VALID",
            "backtest consumption requires a VALID dataset without unresolved issues",
            False,
            "dataset_read",
            {
                "validation_status": descriptor.validation_status.value,
                "unresolved_issues": list(descriptor.unresolved_issues),
            },
        )
    if consumer not in descriptor.compatible_consumers:
        raise RegistryError(
            "DATASET_CONSUMER_INCOMPATIBLE",
            "dataset descriptor does not permit the requested consumer",
            False,
            "dataset_read",
            {"consumer": consumer},
        )
    if DataType.KLINES not in descriptor.data_types:
        raise RegistryError(
            "DATASET_KLINES_MISSING",
            "requested dataset does not contain traded Kline data",
            False,
            "dataset_read",
        )
    if normalized_timeframe not in descriptor.timeframes:
        raise RegistryError(
            "DATASET_TIMEFRAME_MISSING",
            "requested Kline timeframe is not registered",
            False,
            "dataset_read",
            {"timeframe": normalized_timeframe},
        )

    integrity = verify_registered_dataset(root, descriptor_payload, deep=deep)
    if integrity.get("status") != "ok":
        raise StorageError(
            "DATASET_INTEGRITY_FAILED",
            "registered dataset failed integrity validation before consumption",
            False,
            "dataset_read",
            {"issues": integrity.get("issues", [])},
        )
    manifest = load_manifest(root, descriptor)
    if manifest.validation.status is not ValidationStatus.VALID:
        raise StorageError(
            "DATASET_MANIFEST_NOT_VALID",
            "registered manifest is not VALID",
            False,
            "dataset_read",
        )
    partitions = tuple(
        sorted(
            (
                item
                for item in manifest.partitions
                if item.data_type is DataType.KLINES and item.timeframe == normalized_timeframe
            ),
            key=lambda item: (item.start, item.end, item.partition_id),
        )
    )
    if not partitions:
        raise StorageError(
            "DATASET_PARTITIONS_MISSING",
            "registered dataset has no matching Kline partitions",
            False,
            "dataset_read",
        )
    for previous, current in zip(partitions, partitions[1:], strict=False):
        if previous.end != current.start:
            raise StorageError(
                "DATASET_PARTITION_CONTINUITY_FAILED",
                "registered Kline partitions are not exactly contiguous",
                False,
                "dataset_read",
                {
                    "previous": previous.partition_id,
                    "current": current.partition_id,
                    "previous_end": previous.end.isoformat(),
                    "current_start": current.start.isoformat(),
                },
            )
    if partitions[0].start < descriptor.start or partitions[-1].end > descriptor.end:
        raise StorageError(
            "DATASET_PARTITION_BOUNDS_INVALID",
            "selected partitions escape descriptor time bounds",
            False,
            "dataset_read",
        )
    return RegisteredKlineSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        timeframe=normalized_timeframe,
        partitions=partitions,
    )


def _partition_records(
    selection: RegisteredKlineSelection,
    partition: NormalizedPartitionDescriptor,
) -> Iterator[KlineRecord]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise StorageError(
            "PARQUET_DEPENDENCY_MISSING",
            "registered Kline reading requires pyarrow; install the parquet extra",
            False,
            "dataset_read",
        ) from exc

    try:
        path = resolve_registered_path(
            selection.storage_root,
            partition.storage_uri,
            kind="partition",
        )
    except RegisteredPathError as exc:
        raise StorageError(
            "DATASET_PARTITION_PATH_UNSAFE",
            "registered Kline partition path is unsafe",
            False,
            "dataset_read",
            {"partition_id": partition.partition_id},
        ) from exc
    if not path.is_file():
        raise StorageError(
            "DATASET_PARTITION_MISSING",
            "registered Kline partition is missing",
            False,
            "dataset_read",
            {"partition_id": partition.partition_id},
        )
    before = sha256_file(path)
    if before != partition.checksum_sha256:
        raise StorageError(
            "DATASET_PARTITION_CHECKSUM_MISMATCH",
            "registered Kline partition checksum differs before reading",
            False,
            "dataset_read",
            {"partition_id": partition.partition_id},
        )

    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != _KLINE_COLUMNS:
        raise StorageError(
            "DATASET_PARTITION_SCHEMA_MISMATCH",
            "registered Kline partition schema differs from the canonical schema",
            False,
            "dataset_read",
            {"partition_id": partition.partition_id},
        )
    observed = 0
    for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, columns=list(_KLINE_COLUMNS)):
        for raw in batch.to_pylist():
            record = KlineRecord(
                open_time=raw["open_time"],
                close_time=raw["close_time"],
                open=str(raw["open"]),
                high=str(raw["high"]),
                low=str(raw["low"]),
                close=str(raw["close"]),
                volume=str(raw["volume"]),
                quote_volume=str(raw["quote_volume"]),
                trade_count=int(raw["trade_count"]),
                taker_buy_base_volume=str(raw["taker_buy_base_volume"]),
                taker_buy_quote_volume=str(raw["taker_buy_quote_volume"]),
                symbol=str(raw["symbol"]),
                timeframe=str(raw["timeframe"]),
                exchange_id=str(raw["exchange_id"]),
                market_type=str(raw["market_type"]),
                source_kind=str(raw["source_kind"]),
                is_closed=bool(raw["is_closed"]),
            )
            if (
                not record.is_closed
                or record.symbol != selection.manifest.instrument.symbol
                or record.timeframe != selection.timeframe
                or record.exchange_id != selection.manifest.exchange.id
                or record.market_type != selection.manifest.market_type.value
            ):
                raise StorageError(
                    "DATASET_PARTITION_RECORD_IDENTITY_MISMATCH",
                    "registered Kline row differs from manifest identity",
                    False,
                    "dataset_read",
                    {"partition_id": partition.partition_id},
                )
            observed += 1
            yield record
    if observed != partition.row_count:
        raise StorageError(
            "DATASET_PARTITION_ROW_COUNT_MISMATCH",
            "registered Kline partition row count differs from its descriptor",
            False,
            "dataset_read",
            {
                "partition_id": partition.partition_id,
                "expected": partition.row_count,
                "observed": observed,
            },
        )
    after = sha256_file(path)
    if after != before:
        raise StorageError(
            "DATASET_PARTITION_CHANGED_DURING_READ",
            "registered Kline partition changed while it was being read",
            False,
            "dataset_read",
            {"partition_id": partition.partition_id},
        )


def iter_registered_klines(
    selection: RegisteredKlineSelection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterator[KlineRecord]:
    start_utc = _utc_boundary(start, "start")
    end_utc = _utc_boundary(end, "end")
    if start_utc is not None and end_utc is not None and end_utc <= start_utc:
        raise ValueError("end must be later than start")

    previous: datetime | None = None
    expected_delta = timeframe_delta(selection.timeframe)
    for partition in selection.partitions:
        if start_utc is not None and partition.end <= start_utc:
            continue
        if end_utc is not None and partition.start >= end_utc:
            continue
        for record in _partition_records(selection, partition):
            if start_utc is not None and record.open_time < start_utc:
                continue
            if end_utc is not None and record.open_time >= end_utc:
                continue
            if previous is not None and record.open_time - previous != expected_delta:
                raise StorageError(
                    "DATASET_STREAM_CONTINUITY_FAILED",
                    "registered Kline stream is not exactly contiguous",
                    False,
                    "dataset_read",
                    {
                        "previous": previous.isoformat(),
                        "current": record.open_time.isoformat(),
                    },
                )
            previous = record.open_time
            yield record
