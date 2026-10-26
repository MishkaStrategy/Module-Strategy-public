from __future__ import annotations

import csv
import hashlib
import os
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from itertools import chain, groupby
from pathlib import Path

from .contracts import InstrumentReference, NormalizedPartitionDescriptor
from .enums import DataType
from .errors import StorageError, StructuredIngestionError
from .records import NormalizedRecord, normalized_record_fieldnames
from .storage import LocalStorage

# Accepted Wave 2D next-registration allocation bound. It is used only as a
# bounded transaction/recovery reserve at the registry owner path; it is not a
# global filesystem watermark and grants no deletion authority.
MARKET_DATA_REGISTRY_RESERVE_BYTES = 24_576


@dataclass(frozen=True, slots=True)
class PartitionPublicationPlan:
    descriptor: NormalizedPartitionDescriptor
    byte_size: int

    def __post_init__(self) -> None:
        if not isinstance(self.byte_size, int) or isinstance(self.byte_size, bool):
            raise TypeError("byte_size must be an integer")
        if self.byte_size <= 0:
            raise ValueError("byte_size must be positive")


class _CountingHashTextSink:
    def __init__(self) -> None:
        self.byte_size = 0
        self._digest = hashlib.sha256()

    def write(self, text: str) -> int:
        if not isinstance(text, str):
            raise TypeError("CSV serializer must write text")
        payload = text.encode("utf-8")
        self.byte_size += len(payload)
        self._digest.update(payload)
        return len(text)

    @property
    def checksum_sha256(self) -> str:
        return self._digest.hexdigest()


def _free_bytes(path: Path) -> int:
    return int(shutil.disk_usage(path).free)


def _same_filesystem(left: Path, right: Path) -> bool:
    return os.stat(left).st_dev == os.stat(right).st_dev


def _allocation_unit(path: Path) -> int:
    stat = os.statvfs(path)
    unit = int(stat.f_frsize or stat.f_bsize)
    return max(unit, 1)


def _allocated_bytes(logical_bytes: int, allocation_unit: int) -> int:
    if logical_bytes <= 0:
        return 0
    return ((logical_bytes + allocation_unit - 1) // allocation_unit) * allocation_unit


def _validate_common(
    *,
    storage: LocalStorage,
    version: int,
    layer: str,
    instrument: InstrumentReference,
    data_type: DataType,
    timeframe: str | None,
    records: Iterable[NormalizedRecord],
    format_name: str,
) -> tuple[NormalizedRecord, Iterator[NormalizedRecord], Path]:
    if layer not in {"normalized", "derived"}:
        raise StorageError(
            "LAYER_INVALID",
            "partition layer must be normalized or derived",
            False,
            "storage",
        )
    if format_name not in {"csv", "parquet"}:
        raise StorageError(
            "FORMAT_UNSUPPORTED",
            f"unsupported storage format: {format_name}",
            False,
            "storage",
        )
    iterator = iter(records)
    try:
        first = next(iterator)
    except StopIteration as exc:
        raise StorageError(
            "EMPTY_PARTITION",
            "cannot plan an empty partition",
            False,
            "storage",
        ) from exc
    storage._validate_partition_record(first, data_type, timeframe)
    path = storage._partition_path(
        version=version,
        layer=layer,
        instrument=instrument,
        data_type=data_type,
        timeframe=timeframe,
        first=first,
        format_name=format_name,
    )
    return first, iterator, path


def _plan_csv_partition(
    *,
    storage: LocalStorage,
    path: Path,
    first: NormalizedRecord,
    remaining: Iterator[NormalizedRecord],
    data_type: DataType,
    timeframe: str | None,
    source_artifact_ids: tuple[str, ...],
) -> PartitionPublicationPlan:
    row_count = 0
    last_end = storage._partition_end(first, timeframe)
    first_month = (first.timestamp.year, first.timestamp.month)
    previous = (first.timestamp, first.end_time)
    fieldnames = normalized_record_fieldnames(data_type)
    if tuple(first.to_row()) != fieldnames:
        raise StorageError(
            "PARTITION_SCHEMA_MISMATCH",
            "first partition record does not match the normalized schema",
            False,
            "storage",
        )

    sink = _CountingHashTextSink()
    writer = csv.DictWriter(sink, fieldnames=list(fieldnames))
    writer.writeheader()
    for index, record in enumerate(chain((first,), remaining)):
        storage._validate_partition_record(record, data_type, timeframe)
        if (record.timestamp.year, record.timestamp.month) != first_month:
            raise StorageError(
                "MONTH_MIX",
                "a monthly partition cannot contain records from another month",
                False,
                "storage",
            )
        current = (record.timestamp, record.end_time)
        if index and current < previous:
            raise StorageError(
                "PARTITION_ORDER_INVALID",
                "partition records must be sorted by timestamp and end time",
                False,
                "storage",
            )
        row = record.to_row()
        if tuple(row) != fieldnames:
            raise StorageError(
                "PARTITION_SCHEMA_MIX",
                "partition records contain inconsistent row schemas",
                False,
                "storage",
            )
        for name, value in tuple(row.items()):
            row[name] = storage._csv_scalar(value)
        writer.writerow(row)
        row_count += 1
        last_end = max(last_end, storage._partition_end(record, timeframe))
        previous = current

    checksum = sink.checksum_sha256
    descriptor = NormalizedPartitionDescriptor(
        partition_id=f"part:{checksum[:24]}",
        data_type=data_type,
        timeframe=timeframe,
        storage_uri=path.relative_to(storage.root).as_posix(),
        checksum_sha256=checksum,
        row_count=row_count,
        start=first.timestamp,
        end=last_end,
        schema_version=1,
        source_artifact_ids=source_artifact_ids,
    )
    return PartitionPublicationPlan(descriptor=descriptor, byte_size=sink.byte_size)


def plan_partition(
    *,
    storage: LocalStorage,
    dataset_id: str,
    version: int,
    layer: str,
    instrument: InstrumentReference,
    data_type: DataType,
    timeframe: str | None,
    records: Iterable[NormalizedRecord],
    source_artifact_ids: tuple[str, ...],
    format_name: str = "parquet",
    compression: str = "zstd",
) -> PartitionPublicationPlan:
    if not isinstance(dataset_id, str) or not dataset_id.strip():
        raise ValueError("dataset_id must be non-empty")
    first, iterator, path = _validate_common(
        storage=storage,
        version=version,
        layer=layer,
        instrument=instrument,
        data_type=data_type,
        timeframe=timeframe,
        records=records,
        format_name=format_name,
    )
    if format_name == "csv":
        return _plan_csv_partition(
            storage=storage,
            path=path,
            first=first,
            remaining=iterator,
            data_type=data_type,
            timeframe=timeframe,
            source_artifact_ids=source_artifact_ids,
        )

    rows = (first, *tuple(iterator))
    storage._validate_partition_sequence(rows, data_type, timeframe)
    payload = storage._parquet_bytes(rows, compression)
    checksum = hashlib.sha256(payload).hexdigest()
    descriptor = storage._partition_descriptor(
        published=path,
        checksum=checksum,
        rows=rows,
        data_type=data_type,
        timeframe=timeframe,
        source_artifact_ids=source_artifact_ids,
    )
    return PartitionPublicationPlan(descriptor=descriptor, byte_size=len(payload))


def plan_monthly_partitions(
    *,
    storage: LocalStorage,
    dataset_id: str,
    version: int,
    layer: str,
    instrument: InstrumentReference,
    data_type: DataType,
    timeframe: str | None,
    records: Iterable[NormalizedRecord],
    source_artifact_ids: tuple[str, ...],
    format_name: str,
    compression: str,
) -> tuple[PartitionPublicationPlan, ...]:
    iterator = iter(records)
    try:
        first = next(iterator)
    except StopIteration:
        return ()

    def ordered_records() -> Iterable[NormalizedRecord]:
        previous = (first.timestamp, first.end_time)
        yield first
        for item in iterator:
            current = (item.timestamp, item.end_time)
            if current < previous:
                raise StructuredIngestionError(
                    "PARTITION_ORDER_INVALID",
                    "monthly publication input must be timestamp ordered",
                    False,
                    "storage",
                )
            previous = current
            yield item

    output: list[PartitionPublicationPlan] = []
    for _, group in groupby(
        ordered_records(),
        key=lambda item: (item.timestamp.year, item.timestamp.month),
    ):
        output.append(
            plan_partition(
                storage=storage,
                dataset_id=dataset_id,
                version=version,
                layer=layer,
                instrument=instrument,
                data_type=data_type,
                timeframe=timeframe,
                records=group,
                source_artifact_ids=source_artifact_ids,
                format_name=format_name,
                compression=compression,
            )
        )
    return tuple(output)


def _partition_bytes(
    plans: Iterable[PartitionPublicationPlan],
    allocation_unit: int,
) -> tuple[int, int, int]:
    planned = tuple(plans)
    logical_bytes = sum(item.byte_size for item in planned)
    allocated_bytes = sum(_allocated_bytes(item.byte_size, allocation_unit) for item in planned)
    return logical_bytes, allocated_bytes, len(planned)


def require_publication_capacity(
    *,
    storage: LocalStorage,
    registry_path: Path,
    plans: Iterable[PartitionPublicationPlan],
    manifest_byte_size: int,
    format_name: str,
) -> None:
    if format_name not in {"csv", "parquet"}:
        raise ValueError("format_name must be csv or parquet")
    if not isinstance(manifest_byte_size, int) or isinstance(manifest_byte_size, bool):
        raise TypeError("manifest_byte_size must be an integer")
    if manifest_byte_size <= 0:
        raise ValueError("manifest_byte_size must be positive")

    registry_root = registry_path.expanduser().resolve().parent
    shared_filesystem = _same_filesystem(storage.root, registry_root)
    storage_allocation_unit = _allocation_unit(storage.root)
    partition_bytes, partition_allocated_bytes, partition_count = _partition_bytes(
        plans,
        storage_allocation_unit,
    )
    manifest_allocated_bytes = _allocated_bytes(
        manifest_byte_size,
        storage_allocation_unit,
    )
    storage_required = partition_allocated_bytes + manifest_allocated_bytes

    if shared_filesystem:
        registry_allocated_bytes = _allocated_bytes(
            MARKET_DATA_REGISTRY_RESERVE_BYTES,
            storage_allocation_unit,
        )
        required = storage_required + registry_allocated_bytes
        available = _free_bytes(storage.root)
        if available >= required:
            return
        raise StructuredIngestionError(
            "PUBLICATION_CAPACITY_INSUFFICIENT",
            "insufficient storage capacity for the next immutable dataset publication",
            True,
            "storage",
            {
                "available_bytes": available,
                "required_bytes": required,
                "partition_bytes": partition_bytes,
                "partition_allocated_bytes": partition_allocated_bytes,
                "manifest_bytes": manifest_byte_size,
                "manifest_allocated_bytes": manifest_allocated_bytes,
                "registry_reserve_bytes": MARKET_DATA_REGISTRY_RESERVE_BYTES,
                "registry_allocated_bytes": registry_allocated_bytes,
                "allocation_unit_bytes": storage_allocation_unit,
                "partition_count": partition_count,
                "format": format_name,
                "shared_filesystem": True,
            },
        )

    registry_allocation_unit = _allocation_unit(registry_root)
    registry_required = _allocated_bytes(
        MARKET_DATA_REGISTRY_RESERVE_BYTES,
        registry_allocation_unit,
    )
    storage_available = _free_bytes(storage.root)
    registry_available = _free_bytes(registry_root)
    if storage_available >= storage_required and registry_available >= registry_required:
        return
    raise StructuredIngestionError(
        "PUBLICATION_CAPACITY_INSUFFICIENT",
        "insufficient storage capacity for the next immutable dataset publication",
        True,
        "storage",
        {
            "partition_bytes": partition_bytes,
            "partition_allocated_bytes": partition_allocated_bytes,
            "manifest_bytes": manifest_byte_size,
            "manifest_allocated_bytes": manifest_allocated_bytes,
            "partition_count": partition_count,
            "format": format_name,
            "shared_filesystem": False,
            "storage_allocation_unit_bytes": storage_allocation_unit,
            "storage_available_bytes": storage_available,
            "storage_required_bytes": storage_required,
            "registry_allocation_unit_bytes": registry_allocation_unit,
            "registry_available_bytes": registry_available,
            "registry_required_bytes": registry_required,
            "registry_reserve_bytes": MARKET_DATA_REGISTRY_RESERVE_BYTES,
        },
    )


def assert_publication_matches_plan(
    actual: NormalizedPartitionDescriptor,
    planned: PartitionPublicationPlan,
) -> None:
    if actual == planned.descriptor:
        return
    raise StructuredIngestionError(
        "PUBLICATION_PLAN_DRIFT",
        "actual partition publication differed from the pre-publication plan",
        False,
        "storage",
        {
            "planned_partition_id": planned.descriptor.partition_id,
            "actual_partition_id": actual.partition_id,
            "data_type": actual.data_type.value,
            "timeframe": actual.timeframe,
        },
    )
