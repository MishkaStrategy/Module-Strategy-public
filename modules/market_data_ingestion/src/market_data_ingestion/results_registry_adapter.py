from __future__ import annotations

from collections.abc import Iterable

from .canonical import sha256_canonical
from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    NormalizedPartitionDescriptor,
)
from .enums import DataType
from .hashing import dataset_hash
from .version import AGGREGATION_VERSION

_METADATA_TYPES = frozenset({DataType.INSTRUMENT_METADATA, DataType.EXCHANGE_METADATA})


def _partition_lineage_hash(
    partitions: Iterable[NormalizedPartitionDescriptor],
) -> str | None:
    stable = tuple(
        {
            "partition_id": item.partition_id,
            "data_type": item.data_type.value,
            "timeframe": item.timeframe,
            "start": item.start,
            "end": item.end,
            "row_count": item.row_count,
            "checksum_sha256": item.checksum_sha256,
            "schema_version": item.schema_version,
            "source_artifact_ids": item.source_artifact_ids,
        }
        for item in sorted(
            partitions,
            key=lambda value: (
                value.data_type.value,
                value.timeframe or "",
                value.start,
                value.end,
                value.partition_id,
            ),
        )
    )
    return sha256_canonical(stable) if stable else None


def _primary_data_type(manifest: DatasetManifest) -> DataType:
    if DataType.KLINES in manifest.data_types:
        return DataType.KLINES
    for data_type in manifest.data_types:
        if data_type not in _METADATA_TYPES:
            return data_type
    return manifest.data_types[0]


def _validate_identity(
    descriptor: DatasetDescriptor,
    manifest: DatasetManifest,
) -> None:
    expected = {
        "dataset_id": manifest.dataset_id,
        "version": manifest.version,
        "exchange_id": manifest.exchange.id,
        "market_type": manifest.market_type,
        "instrument_id": manifest.instrument.canonical_id,
        "data_types": manifest.data_types,
        "timeframes": manifest.timeframes,
        "start": manifest.start,
        "end": manifest.end,
        "schema_version": manifest.schema_version,
    }
    for field_name, expected_value in expected.items():
        if getattr(descriptor, field_name) != expected_value:
            raise ValueError(f"descriptor and manifest differ at {field_name}")
    if dataset_hash(manifest) != descriptor.dataset_hash:
        raise ValueError("descriptor dataset hash differs from manifest content")


def to_results_registry_dataset_record(
    descriptor: DatasetDescriptor,
    manifest: DatasetManifest,
) -> dict[str, object]:
    """Adapt the ingestion contract to Results Aggregator dataset-record v1."""

    _validate_identity(descriptor, manifest)
    primary_data_type = _primary_data_type(manifest)
    candidate_partitions = tuple(
        item for item in manifest.partitions if item.data_type is primary_data_type
    )
    partition_timeframes = sorted(
        {item.timeframe for item in candidate_partitions if item.timeframe is not None}
    )
    primary_timeframe = (
        descriptor.timeframes[0]
        if descriptor.timeframes
        else partition_timeframes[0]
        if partition_timeframes
        else "event"
    )
    primary_partitions = tuple(
        item
        for item in candidate_partitions
        if item.timeframe is None or item.timeframe == primary_timeframe
    )
    metadata_partitions = tuple(
        item for item in manifest.partitions if item.data_type in _METADATA_TYPES
    )
    funding_partitions = tuple(
        item for item in manifest.partitions if item.data_type is DataType.FUNDING_RATE
    )
    metadata_hash = _partition_lineage_hash(metadata_partitions)
    if metadata_hash is None:
        raise ValueError("manifest has no metadata partitions")
    gap_summary = {
        "count": len(manifest.validation.gaps),
        "unresolved": sum(item.missing_intervals for item in manifest.validation.gaps),
    }
    duplicate_summary = {
        "count": len(manifest.validation.duplicates),
        "conflicting": sum(not item.exact for item in manifest.validation.duplicates),
    }
    return {
        "dataset_id": f"{descriptor.dataset_id}:v{descriptor.version}",
        "source": descriptor.exchange_id,
        "market": descriptor.market_type.value,
        "market_type": descriptor.market_type.value,
        "instrument": descriptor.instrument_id,
        "timeframe": primary_timeframe,
        "min_timestamp": descriptor.start.isoformat(),
        "max_timestamp": descriptor.end.isoformat(),
        "row_count": sum(item.row_count for item in primary_partitions),
        "sha256": descriptor.dataset_hash,
        "preprocessing_version": manifest.normalization_version,
        "aggregation_version": AGGREGATION_VERSION,
        "metadata_hash": metadata_hash,
        "funding_data_hash": _partition_lineage_hash(funding_partitions),
        "gap_summary": gap_summary,
        "duplicate_summary": duplicate_summary,
        "quality_status": descriptor.validation_status.value,
        "timezone_name": "UTC",
    }
