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
    RawArtifactDescriptor,
)
from .enums import DataType, ValidationStatus
from .errors import RegistryError, StorageError
from .integrity import RegisteredPathError, resolve_registered_path, verify_registered_dataset
from .manifest_io import load_manifest
from .records import PriceKlineRecord, normalized_record_fieldnames, normalized_record_from_row
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .timeframes import timeframe_delta, timeframe_seconds

_BATCH_SIZE = 65_536
_PRICE_TYPES = frozenset({DataType.MARK_PRICE, DataType.INDEX_PRICE, DataType.PREMIUM_INDEX})
_CONSUMABLE_VALIDATION_STATUSES = frozenset(
    (ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS)
)


@dataclass(frozen=True, slots=True)
class RegisteredPriceSelection:
    """Immutable registered non-traded price history and producer lineage evidence."""

    storage_root: Path
    descriptor: DatasetDescriptor
    manifest: DatasetManifest
    price_type: DataType
    timeframe: str
    partitions: tuple[NormalizedPartitionDescriptor, ...]
    source_artifacts: tuple[RawArtifactDescriptor, ...]

    @property
    def row_count(self) -> int:
        return sum(item.row_count for item in self.partitions)

    @property
    def source_record_count(self) -> int:
        counts = tuple(item.record_count for item in self.source_artifacts)
        if any(item is None for item in counts):
            raise StorageError(
                "PRICE_SOURCE_COUNT_MISSING",
                "registered price lineage is missing source record-count evidence",
                False,
                "price_dataset_read",
            )
        return sum(int(item) for item in counts if item is not None)

    @property
    def partition_start_inclusive(self) -> datetime:
        return self.partitions[0].start

    @property
    def partition_end_exclusive(self) -> datetime:
        return self.partitions[-1].end

    @property
    def source_coverage_start_inclusive(self) -> datetime:
        return min(item.start for item in self.source_artifacts)

    @property
    def source_coverage_end_exclusive(self) -> datetime:
        return max(item.end for item in self.source_artifacts)

    @property
    def allowed_source_kinds(self) -> frozenset[str]:
        return frozenset(item.source.value for item in self.source_artifacts)

    def to_payload(self) -> dict[str, object]:
        return {
            "dataset_id": self.descriptor.dataset_id,
            "version": self.descriptor.version,
            "dataset_hash": self.descriptor.dataset_hash,
            "instrument_id": self.descriptor.instrument_id,
            "symbol": self.manifest.instrument.symbol,
            "exchange_id": self.manifest.exchange.id,
            "market_type": self.manifest.market_type.value,
            "data_type": self.price_type.value,
            "timeframe": self.timeframe,
            "partition_count": len(self.partitions),
            "row_count": self.row_count,
            "source_record_count": self.source_record_count,
            "partition_start_inclusive": self.partition_start_inclusive.isoformat(),
            "partition_end_exclusive": self.partition_end_exclusive.isoformat(),
            "source_coverage_start_inclusive": self.source_coverage_start_inclusive.isoformat(),
            "source_coverage_end_exclusive": self.source_coverage_end_exclusive.isoformat(),
            "coverage_semantics": "[start,end)",
            "validation_status": self.descriptor.validation_status.value,
            "source_artifacts": [
                {
                    "artifact_id": item.artifact_id,
                    "source": item.source.value,
                    "checksum_sha256": item.checksum_sha256,
                    "record_count": item.record_count,
                    "start": item.start.isoformat(),
                    "end": item.end.isoformat(),
                }
                for item in self.source_artifacts
            ],
        }


def resolve_registered_price_dataset(
    storage_root: Path,
    dataset_id: str,
    version: int,
    *,
    price_type: DataType,
    timeframe: str,
    consumer: str = "modular_backtest",
    deep: bool = True,
) -> RegisteredPriceSelection:
    """Resolve one exact registered mark/index/premium-index series without mutation."""

    root = _validated_storage_root(storage_root)
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError("dataset version must be a positive integer")
    if price_type not in _PRICE_TYPES:
        raise ValueError("price_type must be mark_price, index_price, or premium_index")
    normalized_timeframe = timeframe.strip().lower()
    timeframe_seconds(normalized_timeframe)

    registry_path = root / "registry.sqlite3"
    if registry_path.is_symlink() or not registry_path.is_file():
        raise RegistryError(
            "DATASET_REGISTRY_MISSING",
            "registered dataset registry is missing or unsafe",
            False,
            "price_dataset_read",
        )
    descriptor_payload = SQLiteDatasetRegistry(registry_path).inspect(dataset_id, version)
    if descriptor_payload is None:
        raise RegistryError(
            "DATASET_NOT_FOUND",
            "requested dataset identity is not registered",
            False,
            "price_dataset_read",
            {"dataset_id": dataset_id, "version": version},
        )
    descriptor = descriptor_from_dict(descriptor_payload)
    if descriptor.version != version or descriptor.dataset_id != dataset_id:
        raise RegistryError(
            "DATASET_IDENTITY_MISMATCH",
            "registry returned a descriptor with a different identity",
            False,
            "price_dataset_read",
        )
    if descriptor.storage_uri != root.as_uri():
        raise RegistryError(
            "DATASET_STORAGE_IDENTITY_MISMATCH",
            "descriptor storage URI differs from the configured storage root",
            False,
            "price_dataset_read",
        )
    if (
        descriptor.validation_status not in _CONSUMABLE_VALIDATION_STATUSES
        or descriptor.unresolved_issues
    ):
        raise RegistryError(
            "DATASET_NOT_VALID",
            "price consumption requires a VALID or VALID_WITH_WARNINGS dataset "
            "without unresolved issues",
            False,
            "price_dataset_read",
        )
    if consumer not in descriptor.compatible_consumers:
        raise RegistryError(
            "DATASET_CONSUMER_INCOMPATIBLE",
            "dataset descriptor does not permit the requested consumer",
            False,
            "price_dataset_read",
            {"consumer": consumer},
        )
    if price_type not in descriptor.data_types:
        raise RegistryError(
            "DATASET_PRICE_TYPE_MISSING",
            "requested non-traded price data type is not registered",
            False,
            "price_dataset_read",
            {"data_type": price_type.value},
        )
    if normalized_timeframe not in descriptor.timeframes:
        raise RegistryError(
            "DATASET_TIMEFRAME_MISSING",
            "requested non-traded price timeframe is not registered",
            False,
            "price_dataset_read",
            {"timeframe": normalized_timeframe},
        )

    integrity = verify_registered_dataset(root, descriptor_payload, deep=deep)
    if integrity.get("status") != "ok":
        raise StorageError(
            "DATASET_INTEGRITY_FAILED",
            "registered dataset failed integrity validation before price consumption",
            False,
            "price_dataset_read",
            {"issues": integrity.get("issues", [])},
        )
    manifest = load_manifest(root, descriptor)
    if manifest.validation.status not in _CONSUMABLE_VALIDATION_STATUSES:
        raise StorageError(
            "DATASET_MANIFEST_NOT_VALID",
            "registered manifest is not VALID or VALID_WITH_WARNINGS",
            False,
            "price_dataset_read",
        )

    partitions = tuple(
        sorted(
            (
                item
                for item in manifest.partitions
                if item.data_type is price_type and item.timeframe == normalized_timeframe
            ),
            key=lambda item: (item.start, item.end, item.partition_id),
        )
    )
    if not partitions:
        raise StorageError(
            "DATASET_PRICE_PARTITIONS_MISSING",
            "registered dataset has no matching non-traded price partitions",
            False,
            "price_dataset_read",
        )
    if any(item.row_count <= 0 for item in partitions):
        raise StorageError(
            "PRICE_PARTITION_EMPTY",
            "registered non-traded price partitions must contain at least one row",
            False,
            "price_dataset_read",
        )
    for previous, current in zip(partitions, partitions[1:], strict=False):
        if previous.end != current.start:
            raise StorageError(
                "PRICE_PARTITION_CONTINUITY_FAILED",
                "registered non-traded price partitions are not exactly contiguous",
                False,
                "price_dataset_read",
                {
                    "previous": previous.partition_id,
                    "current": current.partition_id,
                    "previous_end": previous.end.isoformat(),
                    "current_start": current.start.isoformat(),
                },
            )
    if partitions[0].start < descriptor.start or partitions[-1].end > descriptor.end:
        raise StorageError(
            "PRICE_PARTITION_BOUNDS_INVALID",
            "selected non-traded price partitions escape descriptor time bounds",
            False,
            "price_dataset_read",
        )

    source_artifacts = _resolve_raw_lineage(manifest, partitions)
    _assert_source_coverage_contiguous(source_artifacts)
    selection = RegisteredPriceSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        price_type=price_type,
        timeframe=normalized_timeframe,
        partitions=partitions,
        source_artifacts=source_artifacts,
    )
    if selection.partition_start_inclusive < selection.source_coverage_start_inclusive:
        raise StorageError(
            "PRICE_PARTITION_SPAN_OUTSIDE_SOURCE_COVERAGE",
            "normalized price span starts before immutable source coverage",
            False,
            "price_dataset_read",
        )
    if selection.partition_end_exclusive > selection.source_coverage_end_exclusive:
        raise StorageError(
            "PRICE_PARTITION_SPAN_OUTSIDE_SOURCE_COVERAGE",
            "normalized price span ends after immutable source coverage",
            False,
            "price_dataset_read",
        )
    if selection.source_record_count != selection.row_count:
        raise StorageError(
            "PRICE_SOURCE_COUNT_MISMATCH",
            "normalized price row count differs from immutable raw lineage count",
            False,
            "price_dataset_read",
            {"normalized": selection.row_count, "source": selection.source_record_count},
        )
    return selection


def iter_registered_prices(
    selection: RegisteredPriceSelection,
    *,
    start: datetime | None = None,
    end: datetime | None = None,
) -> Iterator[PriceKlineRecord]:
    """Stream an exact contiguous `[start,end)` price window from registered evidence."""

    start_utc = _utc_boundary(start, "start")
    end_utc = _utc_boundary(end, "end")
    if start_utc is not None and end_utc is not None and end_utc <= start_utc:
        raise ValueError("end must be later than start")
    requested_start = start_utc or selection.partition_start_inclusive
    requested_end = end_utc or selection.partition_end_exclusive
    if requested_start < selection.partition_start_inclusive:
        raise StorageError(
            "PRICE_REQUEST_BEFORE_COVERAGE",
            "requested price range starts before registered partition coverage",
            False,
            "price_dataset_read",
        )
    if requested_end > selection.partition_end_exclusive:
        raise StorageError(
            "PRICE_REQUEST_AFTER_COVERAGE",
            "requested price range ends after registered partition coverage",
            False,
            "price_dataset_read",
        )

    previous: datetime | None = None
    first: datetime | None = None
    last: datetime | None = None
    observed = 0
    expected_delta = timeframe_delta(selection.timeframe)
    for partition in selection.partitions:
        if partition.end <= requested_start or partition.start >= requested_end:
            continue
        for record in _price_partition_records(selection, partition):
            if record.open_time < requested_start or record.open_time >= requested_end:
                continue
            if first is None:
                first = record.open_time
            if previous is not None:
                if record.open_time == previous:
                    raise StorageError(
                        "PRICE_TIMESTAMP_DUPLICATE",
                        "registered non-traded price stream contains a duplicate open timestamp",
                        False,
                        "price_dataset_read",
                    )
                if record.open_time - previous != expected_delta:
                    raise StorageError(
                        "PRICE_STREAM_CONTINUITY_FAILED",
                        "registered non-traded price stream is not exactly contiguous",
                        False,
                        "price_dataset_read",
                        {"previous": previous.isoformat(), "current": record.open_time.isoformat()},
                    )
            previous = record.open_time
            last = record.open_time
            observed += 1
            yield record

    if first != requested_start:
        raise StorageError(
            "PRICE_REQUEST_START_NOT_COVERED",
            "registered price rows do not start at the requested boundary",
            False,
            "price_dataset_read",
        )
    if last is None or last + expected_delta != requested_end:
        raise StorageError(
            "PRICE_REQUEST_END_NOT_COVERED",
            "registered price rows do not reach the requested exclusive end boundary",
            False,
            "price_dataset_read",
        )
    expected_rows = int((requested_end - requested_start) / expected_delta)
    if observed != expected_rows:
        raise StorageError(
            "PRICE_REQUEST_ROW_COUNT_MISMATCH",
            "registered price row count differs from the exact requested window",
            False,
            "price_dataset_read",
            {"expected": expected_rows, "observed": observed},
        )


def _price_partition_records(
    selection: RegisteredPriceSelection,
    partition: NormalizedPartitionDescriptor,
) -> Iterator[PriceKlineRecord]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise StorageError(
            "PARQUET_DEPENDENCY_MISSING",
            "registered non-traded price reading requires pyarrow; install the parquet extra",
            False,
            "price_dataset_read",
        ) from exc

    try:
        path = resolve_registered_path(
            selection.storage_root,
            partition.storage_uri,
            kind="non-traded price partition",
        )
    except RegisteredPathError as exc:
        raise StorageError(
            "PRICE_PARTITION_PATH_UNSAFE",
            "registered non-traded price partition path is unsafe",
            False,
            "price_dataset_read",
            {"partition_id": partition.partition_id},
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise StorageError(
            "PRICE_PARTITION_MISSING",
            "registered non-traded price partition is missing or unsafe",
            False,
            "price_dataset_read",
            {"partition_id": partition.partition_id},
        )
    before = sha256_file(path)
    if before != partition.checksum_sha256:
        raise StorageError(
            "PRICE_PARTITION_CHECKSUM_MISMATCH",
            "registered non-traded price partition checksum differs before reading",
            False,
            "price_dataset_read",
            {"partition_id": partition.partition_id},
        )

    columns = normalized_record_fieldnames(selection.price_type)
    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != columns:
        raise StorageError(
            "PRICE_PARTITION_SCHEMA_MISMATCH",
            "registered non-traded price partition schema differs from the canonical schema",
            False,
            "price_dataset_read",
            {"partition_id": partition.partition_id},
        )

    observed = 0
    for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, columns=list(columns)):
        for raw in batch.to_pylist():
            normalized = normalized_record_from_row(selection.price_type, raw)
            if not isinstance(normalized, PriceKlineRecord):
                raise StorageError(
                    "PRICE_PARTITION_RECORD_TYPE_INVALID",
                    "canonical price decoder returned a non-price record",
                    False,
                    "price_dataset_read",
                    {"partition_id": partition.partition_id},
                )
            record = normalized
            if (
                record.data_type is not selection.price_type
                or not record.is_closed
                or record.symbol != selection.manifest.instrument.symbol
                or record.timeframe != selection.timeframe
                or record.exchange_id != selection.manifest.exchange.id
                or record.market_type != selection.manifest.market_type.value
                or record.source_kind not in selection.allowed_source_kinds
            ):
                raise StorageError(
                    "PRICE_PARTITION_RECORD_IDENTITY_MISMATCH",
                    "registered non-traded price row differs from manifest/lineage identity",
                    False,
                    "price_dataset_read",
                    {"partition_id": partition.partition_id},
                )
            if record.open_time < partition.start or record.open_time >= partition.end:
                raise StorageError(
                    "PRICE_PARTITION_RECORD_BOUNDS_INVALID",
                    "registered non-traded price row falls outside partition bounds",
                    False,
                    "price_dataset_read",
                    {"partition_id": partition.partition_id},
                )
            observed += 1
            yield record
    if observed != partition.row_count:
        raise StorageError(
            "PRICE_PARTITION_ROW_COUNT_MISMATCH",
            "registered non-traded price partition row count differs from its descriptor",
            False,
            "price_dataset_read",
            {
                "partition_id": partition.partition_id,
                "expected": partition.row_count,
                "observed": observed,
            },
        )
    after = sha256_file(path)
    if after != before:
        raise StorageError(
            "PRICE_PARTITION_CHANGED_DURING_READ",
            "registered non-traded price partition changed while it was being read",
            False,
            "price_dataset_read",
            {"partition_id": partition.partition_id},
        )


def _resolve_raw_lineage(
    manifest: DatasetManifest,
    partitions: tuple[NormalizedPartitionDescriptor, ...],
) -> tuple[RawArtifactDescriptor, ...]:
    raw_by_id = {item.artifact_id: item for item in manifest.source_artifacts}
    partition_by_id = {item.partition_id: item for item in manifest.partitions}
    resolved: dict[str, RawArtifactDescriptor] = {}

    def visit(reference: str, stack: frozenset[str]) -> None:
        raw = raw_by_id.get(reference)
        if raw is not None:
            resolved[raw.artifact_id] = raw
            return
        partition = partition_by_id.get(reference)
        if partition is None:
            raise StorageError(
                "DATASET_LINEAGE_REFERENCE_UNKNOWN",
                "registered price partition lineage contains an unknown reference",
                False,
                "price_dataset_read",
                {"reference": reference},
            )
        if reference in stack:
            raise StorageError(
                "DATASET_LINEAGE_CYCLE",
                "registered price partition lineage contains a cycle",
                False,
                "price_dataset_read",
                {"reference": reference},
            )
        next_stack = stack | {reference}
        for parent in partition.source_artifact_ids:
            visit(parent, next_stack)

    for partition in partitions:
        for reference in partition.source_artifact_ids:
            visit(reference, frozenset({partition.partition_id}))
    if not resolved:
        raise StorageError(
            "PRICE_LINEAGE_MISSING",
            "registered price partitions do not resolve to immutable raw source artifacts",
            False,
            "price_dataset_read",
        )
    artifacts = tuple(resolved[key] for key in sorted(resolved))
    if any(item.record_count is None for item in artifacts):
        raise StorageError(
            "PRICE_SOURCE_COUNT_MISSING",
            "registered price raw lineage must include exact record counts",
            False,
            "price_dataset_read",
        )
    return artifacts


def _assert_source_coverage_contiguous(artifacts: tuple[RawArtifactDescriptor, ...]) -> None:
    ordered = tuple(sorted(artifacts, key=lambda item: (item.start, item.end, item.artifact_id)))
    coverage_end = ordered[0].end
    for current in ordered[1:]:
        if current.start > coverage_end:
            raise StorageError(
                "PRICE_SOURCE_COVERAGE_GAP",
                "price raw lineage contains an uncovered acquisition interval",
                False,
                "price_dataset_read",
                {
                    "covered_until": coverage_end.isoformat(),
                    "next_start": current.start.isoformat(),
                },
            )
        if current.end > coverage_end:
            coverage_end = current.end


def _validated_storage_root(value: Path) -> Path:
    requested = value.expanduser()
    if requested.is_symlink():
        raise StorageError(
            "DATASET_STORAGE_SYMLINK",
            "registered dataset storage root must not be a symbolic link",
            False,
            "price_dataset_read",
        )
    try:
        root = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise StorageError(
            "DATASET_STORAGE_MISSING",
            "registered dataset storage root does not exist",
            False,
            "price_dataset_read",
        ) from exc
    if not root.is_dir():
        raise StorageError(
            "DATASET_STORAGE_INVALID",
            "registered dataset storage root must be a directory",
            False,
            "price_dataset_read",
        )
    return root


def _utc_boundary(value: datetime | None, name: str) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)
