from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
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
from .records import FundingRateRecord, normalized_record_fieldnames, normalized_record_from_row
from .registry import SQLiteDatasetRegistry, descriptor_from_dict

_BATCH_SIZE = 65_536
_FUNDING_COLUMNS = normalized_record_fieldnames(DataType.FUNDING_RATE)
_CONSUMABLE_VALIDATION_STATUSES = frozenset(
    (ValidationStatus.VALID, ValidationStatus.VALID_WITH_WARNINGS)
)


@dataclass(frozen=True, slots=True)
class RegisteredFundingSelection:
    """Immutable registered funding history and its producer completeness evidence."""

    storage_root: Path
    descriptor: DatasetDescriptor
    manifest: DatasetManifest
    partitions: tuple[NormalizedPartitionDescriptor, ...]
    source_artifacts: tuple[RawArtifactDescriptor, ...]

    @property
    def event_count(self) -> int:
        return sum(item.row_count for item in self.partitions)

    @property
    def source_record_count(self) -> int:
        counts = tuple(item.record_count for item in self.source_artifacts)
        if any(item is None for item in counts):
            raise StorageError(
                "FUNDING_SOURCE_COUNT_MISSING",
                "registered funding lineage is missing source record-count evidence",
                False,
                "funding_dataset_read",
            )
        return sum(int(item) for item in counts if item is not None)

    @property
    def coverage_start_inclusive(self) -> datetime:
        return min(item.start for item in self.source_artifacts)

    @property
    def coverage_end_exclusive(self) -> datetime:
        return max(item.end for item in self.source_artifacts)

    @property
    def event_span_start_inclusive(self) -> datetime:
        return self.partitions[0].start

    @property
    def event_span_end_exclusive(self) -> datetime:
        return self.partitions[-1].end

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
            "data_type": DataType.FUNDING_RATE.value,
            "partition_count": len(self.partitions),
            "event_count": self.event_count,
            "source_record_count": self.source_record_count,
            "coverage_start_inclusive": self.coverage_start_inclusive.isoformat(),
            "coverage_end_exclusive": self.coverage_end_exclusive.isoformat(),
            "coverage_semantics": "[start,end)",
            "coverage_basis": "raw_source_artifact_intervals",
            "event_span_start_inclusive": self.event_span_start_inclusive.isoformat(),
            "event_span_end_exclusive": self.event_span_end_exclusive.isoformat(),
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


def resolve_registered_funding_dataset(
    storage_root: Path,
    dataset_id: str,
    version: int,
    *,
    consumer: str = "modular_backtest",
    deep: bool = True,
) -> RegisteredFundingSelection:
    """Resolve one exact registered funding dataset without mutating registry state."""

    root = _validated_storage_root(storage_root)
    if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
        raise ValueError("dataset version must be a positive integer")

    registry_path = root / "registry.sqlite3"
    if registry_path.is_symlink() or not registry_path.is_file():
        raise RegistryError(
            "DATASET_REGISTRY_MISSING",
            "registered dataset registry is missing or unsafe",
            False,
            "funding_dataset_read",
        )
    descriptor_payload = SQLiteDatasetRegistry(registry_path).inspect(dataset_id, version)
    if descriptor_payload is None:
        raise RegistryError(
            "DATASET_NOT_FOUND",
            "requested dataset identity is not registered",
            False,
            "funding_dataset_read",
            {"dataset_id": dataset_id, "version": version},
        )
    descriptor = descriptor_from_dict(descriptor_payload)
    if descriptor.version != version or descriptor.dataset_id != dataset_id:
        raise RegistryError(
            "DATASET_IDENTITY_MISMATCH",
            "registry returned a descriptor with a different identity",
            False,
            "funding_dataset_read",
        )
    if descriptor.storage_uri != root.as_uri():
        raise RegistryError(
            "DATASET_STORAGE_IDENTITY_MISMATCH",
            "descriptor storage URI differs from the configured storage root",
            False,
            "funding_dataset_read",
        )
    if (
        descriptor.validation_status not in _CONSUMABLE_VALIDATION_STATUSES
        or descriptor.unresolved_issues
    ):
        raise RegistryError(
            "DATASET_NOT_VALID",
            "funding consumption requires a VALID or VALID_WITH_WARNINGS dataset "
            "without unresolved issues",
            False,
            "funding_dataset_read",
        )
    if consumer not in descriptor.compatible_consumers:
        raise RegistryError(
            "DATASET_CONSUMER_INCOMPATIBLE",
            "dataset descriptor does not permit the requested consumer",
            False,
            "funding_dataset_read",
            {"consumer": consumer},
        )
    if DataType.FUNDING_RATE not in descriptor.data_types:
        raise RegistryError(
            "DATASET_FUNDING_MISSING",
            "requested dataset does not contain funding-rate data",
            False,
            "funding_dataset_read",
        )

    integrity = verify_registered_dataset(root, descriptor_payload, deep=deep)
    if integrity.get("status") != "ok":
        raise StorageError(
            "DATASET_INTEGRITY_FAILED",
            "registered dataset failed integrity validation before funding consumption",
            False,
            "funding_dataset_read",
            {"issues": integrity.get("issues", [])},
        )
    manifest = load_manifest(root, descriptor)
    if manifest.validation.status not in _CONSUMABLE_VALIDATION_STATUSES:
        raise StorageError(
            "DATASET_MANIFEST_NOT_VALID",
            "registered manifest is not VALID or VALID_WITH_WARNINGS",
            False,
            "funding_dataset_read",
        )

    partitions = tuple(
        sorted(
            (item for item in manifest.partitions if item.data_type is DataType.FUNDING_RATE),
            key=lambda item: (item.start, item.end, item.partition_id),
        )
    )
    if not partitions:
        raise StorageError(
            "DATASET_FUNDING_PARTITIONS_MISSING",
            "registered dataset has no funding-rate partitions",
            False,
            "funding_dataset_read",
        )
    if any(item.row_count <= 0 for item in partitions):
        raise StorageError(
            "FUNDING_PARTITION_EMPTY",
            "registered funding partitions must contain at least one event",
            False,
            "funding_dataset_read",
        )
    for previous, current in zip(partitions, partitions[1:], strict=False):
        if current.start < previous.end:
            raise StorageError(
                "FUNDING_PARTITION_OVERLAP",
                "registered funding partitions overlap",
                False,
                "funding_dataset_read",
                {"previous": previous.partition_id, "current": current.partition_id},
            )
    if partitions[0].start < descriptor.start or partitions[-1].end > descriptor.end:
        raise StorageError(
            "FUNDING_PARTITION_BOUNDS_INVALID",
            "funding partitions escape descriptor time bounds",
            False,
            "funding_dataset_read",
        )

    source_artifacts = _resolve_raw_lineage(manifest, partitions, require_record_counts=True)
    _assert_source_coverage_contiguous(source_artifacts)
    selection = RegisteredFundingSelection(
        storage_root=root,
        descriptor=descriptor,
        manifest=manifest,
        partitions=partitions,
        source_artifacts=source_artifacts,
    )
    if (
        selection.event_span_start_inclusive < selection.coverage_start_inclusive
        or selection.event_span_end_exclusive > selection.coverage_end_exclusive
    ):
        raise StorageError(
            "FUNDING_EVENT_SPAN_OUTSIDE_SOURCE_COVERAGE",
            "normalized funding event span escapes immutable raw acquisition coverage",
            False,
            "funding_dataset_read",
        )
    if selection.source_record_count != selection.event_count:
        raise StorageError(
            "FUNDING_SOURCE_COUNT_MISMATCH",
            "normalized funding event count differs from immutable raw lineage count",
            False,
            "funding_dataset_read",
            {
                "normalized": selection.event_count,
                "source": selection.source_record_count,
            },
        )
    return selection


def iter_registered_funding(selection: RegisteredFundingSelection) -> Iterator[FundingRateRecord]:
    """Stream the complete registered funding history with strict global ordering."""

    previous: datetime | None = None
    observed = 0
    for partition in selection.partitions:
        for record in _funding_partition_records(selection, partition):
            if previous is not None:
                if record.funding_time == previous:
                    raise StorageError(
                        "FUNDING_TIMESTAMP_DUPLICATE",
                        "registered funding stream contains a duplicate timestamp",
                        False,
                        "funding_dataset_read",
                        {"timestamp": record.funding_time.isoformat()},
                    )
                if record.funding_time < previous:
                    raise StorageError(
                        "FUNDING_TIMESTAMP_ORDER_INVALID",
                        "registered funding stream is not strictly ordered",
                        False,
                        "funding_dataset_read",
                    )
            previous = record.funding_time
            observed += 1
            yield record
    if observed != selection.event_count:
        raise StorageError(
            "FUNDING_STREAM_COUNT_MISMATCH",
            "observed funding event count differs from registered partition evidence",
            False,
            "funding_dataset_read",
            {"expected": selection.event_count, "observed": observed},
        )


def _funding_partition_records(
    selection: RegisteredFundingSelection,
    partition: NormalizedPartitionDescriptor,
) -> Iterator[FundingRateRecord]:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise StorageError(
            "PARQUET_DEPENDENCY_MISSING",
            "registered funding reading requires pyarrow; install the parquet extra",
            False,
            "funding_dataset_read",
        ) from exc

    try:
        path = resolve_registered_path(
            selection.storage_root,
            partition.storage_uri,
            kind="funding partition",
        )
    except RegisteredPathError as exc:
        raise StorageError(
            "FUNDING_PARTITION_PATH_UNSAFE",
            "registered funding partition path is unsafe",
            False,
            "funding_dataset_read",
            {"partition_id": partition.partition_id},
        ) from exc
    if path.is_symlink() or not path.is_file():
        raise StorageError(
            "FUNDING_PARTITION_MISSING",
            "registered funding partition is missing or unsafe",
            False,
            "funding_dataset_read",
            {"partition_id": partition.partition_id},
        )
    before = sha256_file(path)
    if before != partition.checksum_sha256:
        raise StorageError(
            "FUNDING_PARTITION_CHECKSUM_MISMATCH",
            "registered funding partition checksum differs before reading",
            False,
            "funding_dataset_read",
            {"partition_id": partition.partition_id},
        )

    parquet = pq.ParquetFile(path)
    if tuple(parquet.schema_arrow.names) != _FUNDING_COLUMNS:
        raise StorageError(
            "FUNDING_PARTITION_SCHEMA_MISMATCH",
            "registered funding partition schema differs from the canonical schema",
            False,
            "funding_dataset_read",
            {"partition_id": partition.partition_id},
        )

    observed = 0
    for batch in parquet.iter_batches(batch_size=_BATCH_SIZE, columns=list(_FUNDING_COLUMNS)):
        for raw in batch.to_pylist():
            normalized = normalized_record_from_row(DataType.FUNDING_RATE, raw)
            if not isinstance(normalized, FundingRateRecord):
                raise StorageError(
                    "FUNDING_PARTITION_RECORD_TYPE_INVALID",
                    "canonical funding decoder returned a non-funding record",
                    False,
                    "funding_dataset_read",
                    {"partition_id": partition.partition_id},
                )
            record = normalized
            if (
                record.symbol != selection.manifest.instrument.symbol
                or record.exchange_id != selection.manifest.exchange.id
                or record.market_type != selection.manifest.market_type.value
                or record.source_kind not in selection.allowed_source_kinds
            ):
                raise StorageError(
                    "FUNDING_PARTITION_RECORD_IDENTITY_MISMATCH",
                    "registered funding row differs from manifest/lineage identity",
                    False,
                    "funding_dataset_read",
                    {"partition_id": partition.partition_id},
                )
            if record.funding_time < partition.start or record.funding_time >= partition.end:
                raise StorageError(
                    "FUNDING_PARTITION_RECORD_BOUNDS_INVALID",
                    "registered funding row falls outside its partition bounds",
                    False,
                    "funding_dataset_read",
                    {"partition_id": partition.partition_id},
                )
            observed += 1
            yield record
    if observed != partition.row_count:
        raise StorageError(
            "FUNDING_PARTITION_ROW_COUNT_MISMATCH",
            "registered funding partition row count differs from its descriptor",
            False,
            "funding_dataset_read",
            {
                "partition_id": partition.partition_id,
                "expected": partition.row_count,
                "observed": observed,
            },
        )
    after = sha256_file(path)
    if after != before:
        raise StorageError(
            "FUNDING_PARTITION_CHANGED_DURING_READ",
            "registered funding partition changed while it was being read",
            False,
            "funding_dataset_read",
            {"partition_id": partition.partition_id},
        )


def _assert_source_coverage_contiguous(
    artifacts: tuple[RawArtifactDescriptor, ...],
) -> None:
    ordered = tuple(sorted(artifacts, key=lambda item: (item.start, item.end, item.artifact_id)))
    coverage_end = ordered[0].end
    for current in ordered[1:]:
        if current.start > coverage_end:
            raise StorageError(
                "FUNDING_SOURCE_COVERAGE_GAP",
                "funding raw lineage contains an uncovered acquisition interval",
                False,
                "funding_dataset_read",
                {
                    "covered_until": coverage_end.isoformat(),
                    "next_start": current.start.isoformat(),
                },
            )
        if current.end > coverage_end:
            coverage_end = current.end


def _resolve_raw_lineage(
    manifest: DatasetManifest,
    partitions: tuple[NormalizedPartitionDescriptor, ...],
    *,
    require_record_counts: bool,
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
                "registered partition lineage contains an unknown reference",
                False,
                "funding_dataset_read",
                {"reference": reference},
            )
        if reference in stack:
            raise StorageError(
                "DATASET_LINEAGE_CYCLE",
                "registered partition lineage contains a cycle",
                False,
                "funding_dataset_read",
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
            "FUNDING_LINEAGE_MISSING",
            "funding partitions do not resolve to immutable raw source artifacts",
            False,
            "funding_dataset_read",
        )
    artifacts = tuple(resolved[key] for key in sorted(resolved))
    if require_record_counts and any(item.record_count is None for item in artifacts):
        raise StorageError(
            "FUNDING_SOURCE_COUNT_MISSING",
            "funding raw lineage must include exact record counts",
            False,
            "funding_dataset_read",
        )
    return artifacts


def _validated_storage_root(value: Path) -> Path:
    requested = value.expanduser()
    if requested.is_symlink():
        raise StorageError(
            "DATASET_STORAGE_SYMLINK",
            "registered dataset storage root must not be a symbolic link",
            False,
            "funding_dataset_read",
        )
    try:
        root = requested.resolve(strict=True)
    except FileNotFoundError as exc:
        raise StorageError(
            "DATASET_STORAGE_MISSING",
            "registered dataset storage root does not exist",
            False,
            "funding_dataset_read",
        ) from exc
    if not root.is_dir():
        raise StorageError(
            "DATASET_STORAGE_INVALID",
            "registered dataset storage root must be a directory",
            False,
            "funding_dataset_read",
        )
    return root
