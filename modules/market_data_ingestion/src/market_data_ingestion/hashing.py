from __future__ import annotations

import hashlib
from collections.abc import Iterable

from .canonical import sha256_canonical
from .contracts import DatasetManifest, NormalizedPartitionDescriptor
from .version import HASH_POLICY_VERSION


def merkle_root(hashes: Iterable[str]) -> str:
    level = [bytes.fromhex(item) for item in hashes]
    if not level:
        return hashlib.sha256(b"").hexdigest()
    while len(level) > 1:
        if len(level) % 2:
            level.append(level[-1])
        level = [
            hashlib.sha256(level[index] + level[index + 1]).digest()
            for index in range(0, len(level), 2)
        ]
    return level[0].hex()


def ordered_partition_root(partitions: Iterable[NormalizedPartitionDescriptor]) -> str:
    ordered = sorted(
        partitions,
        key=lambda item: (
            item.data_type.value,
            item.timeframe or "",
            item.start,
            item.end,
            item.partition_id,
        ),
    )
    return merkle_root(item.checksum_sha256 for item in ordered)


def dataset_hash(manifest: DatasetManifest) -> str:
    """Hash canonical dataset metadata plus ordered partition checksums."""

    ordered_partitions = tuple(
        sorted(
            manifest.partitions,
            key=lambda item: (
                item.data_type.value,
                item.timeframe or "",
                item.start,
                item.end,
                item.partition_id,
            ),
        )
    )
    stable_sources = tuple(
        {
            "artifact_id": item.artifact_id,
            "source": item.source,
            "source_uri": item.source_uri,
            "checksum_sha256": item.checksum_sha256,
            "byte_size": item.byte_size,
            "record_count": item.record_count,
            "start": item.start,
            "end": item.end,
            "adapter_version": item.adapter_version,
            "upstream_checksum": item.response_metadata.get("upstream_checksum"),
        }
        for item in sorted(
            manifest.source_artifacts,
            key=lambda item: (item.artifact_id, item.checksum_sha256),
        )
    )
    partition_lineage = tuple(
        {
            "partition_id": item.partition_id,
            "data_type": item.data_type,
            "timeframe": item.timeframe,
            "checksum_sha256": item.checksum_sha256,
            "row_count": item.row_count,
            "start": item.start,
            "end": item.end,
            "schema_version": item.schema_version,
            "source_artifact_ids": item.source_artifact_ids,
        }
        for item in ordered_partitions
    )
    identity = {
        "hash_policy_version": HASH_POLICY_VERSION,
        "manifest_version": manifest.manifest_version,
        "exchange": manifest.exchange,
        "market_type": manifest.market_type,
        "instrument": manifest.instrument,
        "data_types": manifest.data_types,
        "timeframes": manifest.timeframes,
        "start": manifest.start,
        "end": manifest.end,
        "schema_version": manifest.schema_version,
        "normalization_version": manifest.normalization_version,
        "validation_policy_version": manifest.validation_policy_version,
        "adapter_version": manifest.exchange.adapter_version,
        "partition_root": ordered_partition_root(ordered_partitions),
        "partition_lineage_hash": sha256_canonical(partition_lineage),
        "source_artifact_hash": sha256_canonical(stable_sources),
        "partition_count": len(ordered_partitions),
        "source_artifact_count": len(stable_sources),
    }
    return sha256_canonical(identity)
