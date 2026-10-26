from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .canonical import sha256_file
from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    NormalizedPartitionDescriptor,
)
from .enums import DataType, ValidationStatus
from .errors import RegistryError, StorageError
from .hashing import dataset_hash
from .integrity import RegisteredPathError, resolve_registered_path
from .manifest_io import load_manifest
from .records import KlineRecord
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .timeframes import timeframe_seconds

DEFAULT_STREAM_BATCH_SIZE = 8_192
MAX_STREAM_BATCH_SIZE = 1_000_000
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
_KLINE_COLUMN_POSITIONS = {name: index for index, name in enumerate(_KLINE_COLUMNS)}
KLINE_SCHEMA_FINGERPRINT = hashlib.sha256(
    json.dumps(
        {"columns": _KLINE_COLUMNS, "schema_contract": "registered-klines-v1"},
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
).hexdigest()


@dataclass(frozen=True, slots=True)
class StreamingRegisteredKlineSelection:
    """Manifest-resolved selection whose partition bytes are validated on demand."""

    storage_root: Path
    descriptor: DatasetDescriptor
    manifest: DatasetManifest
    manifest_checksum: str
    timeframe: str
    partitions: tuple[NormalizedPartitionDescriptor, ...]

    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.partitions)


@dataclass(frozen=True, slots=True)
class RegisteredKlineBatch:
    """One bounded, ordered slice from one registered Parquet partition."""

    partition_index: int
    partition_id: str
    row_offset: int
    records: tuple[KlineRecord, ...]

    @property
    def row_count(self) -> int:
        return len(self.records)


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


def _validate_descriptor(
    descriptor: DatasetDescriptor,
    *,
    root: Path,
    dataset_id: str,
    version: int,
    timeframe: str,
    consumer: str,
) -> None:
    if descriptor.dataset_id != dataset_id or descriptor.version != version:
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
    if timeframe not in descriptor.timeframes:
        raise RegistryError(
            "DATASET_TIMEFRAME_MISSING",
            "requested Kline timeframe is not registered",
            False,
            "dataset_read",
            {"timeframe": timeframe},
        )


def _validate_manifest_identity(
    descriptor: DatasetDescriptor,
    manifest: DatasetManifest,
) -> None:
    mismatches: list[str] = []
    checks = {
        "dataset_id": (descriptor.dataset_id, manifest.dataset_id),
        "version": (descriptor.version, manifest.version),
        "dataset_hash": (descriptor.dataset_hash, dataset_hash(manifest)),
        "exchange_id": (descriptor.exchange_id, manifest.exchange.id),
        "market_type": (descriptor.market_type, manifest.market_type),
        "instrument_id": (descriptor.instrument_id, manifest.instrument.canonical_id),
        "data_types": (descriptor.data_types, manifest.data_types),
        "timeframes": (descriptor.timeframes, manifest.timeframes),
        "start": (descriptor.start, manifest.start),
        "end": (descriptor.end, manifest.end),
        "schema_version": (descriptor.schema_version, manifest.schema_version),
        "validation_status": (descriptor.validation_status, manifest.validation.status),
        "lineage": (descriptor.lineage, manifest.lineage),
    }
    for name, (actual, expected) in checks.items():
        if actual != expected:
            mismatches.append(name)
    if mismatches:
        raise StorageError(
            "DATASET_MANIFEST_IDENTITY_MISMATCH",
            "registered manifest identity differs from its registry descriptor",
            False,
            "dataset_read",
            {"fields": mismatches},
        )
    if manifest.validation.status is not ValidationStatus.VALID:
        raise StorageError(
            "DATASET_MANIFEST_NOT_VALID",
            "registered manifest is not VALID",
            False,
            "dataset_read",
        )


def _selected_partitions(
    manifest: DatasetManifest,
    descriptor: DatasetDescriptor,
    timeframe: str,
) -> tuple[NormalizedPartitionDescriptor, ...]:
    declared = tuple(
        item
        for item in manifest.partitions
        if item.data_type is DataType.KLINES and item.timeframe == timeframe
    )
    ordered = tuple(sorted(declared, key=lambda item: (item.start, item.end, item.partition_id)))
    if declared != ordered:
        raise StorageError(
            "DATASET_PARTITION_ORDER_INVALID",
            "registered Kline partitions are not declared in deterministic order",
            False,
            "dataset_read",
        )
    if not ordered:
        raise StorageError(
            "DATASET_PARTITIONS_MISSING",
            "registered dataset has no matching Kline partitions",
            False,
            "dataset_read",
        )
    empty = [item.partition_id for item in ordered if item.row_count == 0]
    if empty:
        raise StorageError(
            "DATASET_PARTITION_EMPTY",
            "registered Kline partitions must not be empty",
            False,
            "dataset_read",
            {"partition_ids": empty},
        )
    for previous, current in zip(ordered, ordered[1:], strict=False):
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
    if ordered[0].start < descriptor.start or ordered[-1].end > descriptor.end:
        raise StorageError(
            "DATASET_PARTITION_BOUNDS_INVALID",
            "selected partitions escape descriptor time bounds",
            False,
            "dataset_read",
        )
    return ordered


def resolve_streaming_registered_kline_dataset(
    storage_root: Path,
    dataset_id: str,
    version: int,
    *,
    timeframe: str = "5m",
    consumer: str = "modular_backtest",
) -> StreamingRegisteredKlineSelection:
    """Resolve registry and manifest identity without pre-reading every partition.

    Partition existence, checksum, schema and row-count validation is intentionally
    deferred to the deterministic partition iterator. This is what permits the first
    candle to become available before the complete dataset has been read.
    """

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
    _validate_descriptor(
        descriptor,
        root=root,
        dataset_id=dataset_id,
        version=version,
        timeframe=normalized_timeframe,
        consumer=consumer,
    )
    try:
        manifest_path = resolve_registered_path(root, descriptor.manifest_uri, kind="manifest")
    except RegisteredPathError as exc:
        raise StorageError(
            "MANIFEST_PATH_ESCAPE",
            "descriptor manifest path is unsafe",
            False,
            "manifest",
        ) from exc
    if not manifest_path.is_file():
        raise StorageError(
            "MANIFEST_MISSING",
            "registered manifest file is missing",
            False,
            "manifest",
        )
    manifest_checksum = sha256_file(manifest_path)
    manifest = load_manifest(root, descriptor)
    if sha256_file(manifest_path) != manifest_checksum:
        raise StorageError(
            "MANIFEST_CHANGED_DURING_READ",
            "registered manifest changed while it was being resolved",
            False,
            "manifest",
        )
    _validate_manifest_identity(descriptor, manifest)
    partitions = _selected_partitions(manifest, descriptor, normalized_timeframe)
    return StreamingRegisteredKlineSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        manifest_checksum=manifest_checksum,
        timeframe=normalized_timeframe,
        partitions=partitions,
    )


def _validated_batch_size(value: int) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ValueError("batch_size must be a positive integer")
    if value > MAX_STREAM_BATCH_SIZE:
        raise ValueError(f"batch_size must not exceed {MAX_STREAM_BATCH_SIZE}")
    return value


def _record_from_batch(batch: Any, row_index: int) -> KlineRecord:
    def value(name: str) -> Any:
        column = batch.column(_KLINE_COLUMN_POSITIONS[name])
        return column[row_index].as_py()

    return KlineRecord(
        open_time=value("open_time"),
        close_time=value("close_time"),
        open=str(value("open")),
        high=str(value("high")),
        low=str(value("low")),
        close=str(value("close")),
        volume=str(value("volume")),
        quote_volume=str(value("quote_volume")),
        trade_count=int(value("trade_count")),
        taker_buy_base_volume=str(value("taker_buy_base_volume")),
        taker_buy_quote_volume=str(value("taker_buy_quote_volume")),
        symbol=str(value("symbol")),
        timeframe=str(value("timeframe")),
        exchange_id=str(value("exchange_id")),
        market_type=str(value("market_type")),
        source_kind=str(value("source_kind")),
        is_closed=bool(value("is_closed")),
    )


def _validate_record_identity(
    selection: StreamingRegisteredKlineSelection,
    partition: NormalizedPartitionDescriptor,
    record: KlineRecord,
) -> None:
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


def _partition_batches(
    selection: StreamingRegisteredKlineSelection,
    partition: NormalizedPartitionDescriptor,
    *,
    partition_index: int,
    batch_size: int,
    start_row_offset: int = 0,
) -> Iterator[RegisteredKlineBatch]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise StorageError(
            "PARQUET_DEPENDENCY_MISSING",
            "registered Kline reading requires pyarrow; install the parquet extra",
            False,
            "dataset_read",
        ) from exc

    normalized_batch_size = _validated_batch_size(batch_size)
    if (
        not isinstance(start_row_offset, int)
        or isinstance(start_row_offset, bool)
        or start_row_offset < 0
        or start_row_offset > partition.row_count
    ):
        raise ValueError("start_row_offset is outside the selected partition")

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

    parquet: Any = None
    observed = 0
    try:
        parquet = pq.ParquetFile(path)
        if tuple(parquet.schema_arrow.names) != _KLINE_COLUMNS:
            raise StorageError(
                "DATASET_PARTITION_SCHEMA_MISMATCH",
                "registered Kline partition schema differs from the canonical schema",
                False,
                "dataset_read",
                {"partition_id": partition.partition_id},
            )
        for batch in parquet.iter_batches(
            batch_size=normalized_batch_size,
            columns=list(_KLINE_COLUMNS),
        ):
            batch_start = observed
            batch_end = batch_start + batch.num_rows
            observed = batch_end
            if batch_end <= start_row_offset:
                continue
            first_row = max(0, start_row_offset - batch_start)
            records: list[KlineRecord] = []
            for row_index in range(first_row, batch.num_rows):
                try:
                    record = _record_from_batch(batch, row_index)
                except (TypeError, ValueError, OverflowError) as exc:
                    raise StorageError(
                        "DATASET_PARTITION_RECORD_INVALID",
                        "registered Kline row is invalid",
                        False,
                        "dataset_read",
                        {
                            "partition_id": partition.partition_id,
                            "row_offset": batch_start + row_index,
                        },
                    ) from exc
                _validate_record_identity(selection, partition, record)
                records.append(record)
            if records:
                yield RegisteredKlineBatch(
                    partition_index=partition_index,
                    partition_id=partition.partition_id,
                    row_offset=batch_start + first_row,
                    records=tuple(records),
                )
    except StorageError:
        raise
    except Exception as exc:
        raise StorageError(
            "DATASET_PARTITION_READ_FAILED",
            "registered Kline partition could not be read",
            False,
            "dataset_read",
            {"partition_id": partition.partition_id, "error_type": type(exc).__name__},
        ) from exc
    finally:
        close = getattr(parquet, "close", None)
        if callable(close):
            close()

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


def iter_registered_kline_batches(
    selection: StreamingRegisteredKlineSelection,
    *,
    batch_size: int = DEFAULT_STREAM_BATCH_SIZE,
    start_partition_index: int = 0,
    start_row_offset: int = 0,
) -> Iterator[RegisteredKlineBatch]:
    """Yield deterministic bounded batches without full table/list materialization."""

    normalized_batch_size = _validated_batch_size(batch_size)
    if (
        not isinstance(start_partition_index, int)
        or isinstance(start_partition_index, bool)
        or start_partition_index < 0
        or start_partition_index > len(selection.partitions)
    ):
        raise ValueError("start_partition_index is outside the selected dataset")
    if start_partition_index == len(selection.partitions):
        if start_row_offset != 0:
            raise ValueError("start_row_offset must be zero at end of dataset")
        return
    first_partition = selection.partitions[start_partition_index]
    if (
        not isinstance(start_row_offset, int)
        or isinstance(start_row_offset, bool)
        or start_row_offset < 0
        or start_row_offset > first_partition.row_count
    ):
        raise ValueError("start_row_offset is outside the selected partition")

    for partition_index in range(start_partition_index, len(selection.partitions)):
        partition = selection.partitions[partition_index]
        row_offset = start_row_offset if partition_index == start_partition_index else 0
        yield from _partition_batches(
            selection,
            partition,
            partition_index=partition_index,
            batch_size=normalized_batch_size,
            start_row_offset=row_offset,
        )
