from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from .canonical import sha256_canonical
from .contracts import NormalizedPartitionDescriptor
from .enums import DataType, GapReason
from .integrity import partition_integrity_issue
from .manifest_io import load_manifest
from .registry import descriptor_from_dict

RepairRangeSource = Literal["partition_integrity", "validation_gap"]

RepairAction = Literal[
    "no_action",
    "gap_range_repair",
    "metadata_local_rebuild",
    "derived_local_rebuild",
    "stream_redownload_and_rebuild",
    "multi_stream_staged_repair",
    "separate_repairs_required",
]

_INTERVAL_GAP_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)
_METADATA_TYPES = frozenset(
    {
        DataType.INSTRUMENT_METADATA,
        DataType.EXCHANGE_METADATA,
    }
)


@dataclass(frozen=True, slots=True)
class RepairAffectedRange:
    data_type: DataType
    timeframe: str | None
    start: datetime
    end: datetime
    source: RepairRangeSource
    partition_id: str | None = None


@dataclass(frozen=True, slots=True)
class RepairPlanPreview:
    dataset_id: str
    version: int
    preview_id: str
    action: RepairAction
    selected_data_type: DataType | None
    checked_partitions: int
    damaged_partitions: int
    unresolved_gaps: int
    affected_ranges: tuple[RepairAffectedRange, ...]
    evidence: tuple[dict[str, object], ...]
    expected_mutations: tuple[str, ...]
    confirmation_required: bool
    requires_network: bool


def preview_registered_dataset_repair(
    storage_root: Path,
    descriptor_payload: dict[str, Any],
    *,
    data_type: DataType | None = None,
) -> RepairPlanPreview:
    """Build a deterministic repair plan without writing files or Registry state."""

    descriptor = descriptor_from_dict(descriptor_payload)
    manifest = load_manifest(storage_root, descriptor)
    candidates = tuple(
        item
        for item in manifest.partitions
        if (item.storage_uri.startswith("normalized/") or item.storage_uri.startswith("derived/"))
        and (data_type is None or item.data_type is data_type)
    )

    damaged: list[NormalizedPartitionDescriptor] = []
    evidence: list[dict[str, object]] = []
    ranges: list[RepairAffectedRange] = []
    for partition in candidates:
        issue = partition_integrity_issue(storage_root, partition)
        if issue is None:
            continue
        damaged.append(partition)
        evidence.append(
            {
                **issue,
                "data_type": partition.data_type.value,
                "timeframe": partition.timeframe,
                "start": partition.start,
                "end": partition.end,
                "layer": _layer(partition),
            }
        )
        ranges.append(
            RepairAffectedRange(
                data_type=partition.data_type,
                timeframe=partition.timeframe,
                start=partition.start,
                end=partition.end,
                source="partition_integrity",
                partition_id=partition.partition_id,
            )
        )

    unknown_gap_entries = tuple(
        (
            report.data_type,
            report.timeframe,
            tuple(gap for gap in report.report.gaps if gap.reason is GapReason.UNKNOWN),
        )
        for report in manifest.data_type_reports
        if (data_type is None or report.data_type is data_type)
    )
    unknown_gap_entries = tuple(item for item in unknown_gap_entries if item[2])
    for gap_type, timeframe, gaps in unknown_gap_entries:
        for gap in gaps:
            evidence.append(
                {
                    "issue": "unresolved_gap",
                    "data_type": gap_type.value,
                    "timeframe": timeframe,
                    "start": gap.start,
                    "end": gap.end,
                    "missing_intervals": gap.missing_intervals,
                    "reason": gap.reason.value,
                }
            )
            ranges.append(
                RepairAffectedRange(
                    data_type=gap_type,
                    timeframe=timeframe,
                    start=gap.start,
                    end=gap.end,
                    source="validation_gap",
                )
            )

    action, requires_network = _classify_action(damaged, unknown_gap_entries)
    affected_ranges = tuple(
        sorted(
            {
                (
                    item.data_type,
                    item.timeframe,
                    item.start,
                    item.end,
                    item.source,
                    item.partition_id,
                ): item
                for item in ranges
            }.values(),
            key=lambda item: (
                item.data_type.value,
                item.timeframe or "",
                item.start,
                item.end,
                item.source,
                item.partition_id or "",
            ),
        )
    )
    ordered_evidence = tuple(
        sorted(
            evidence,
            key=lambda item: (
                str(item.get("data_type", "")),
                str(item.get("timeframe", "")),
                str(item.get("start", "")),
                str(item.get("partition_id", "")),
                str(item.get("issue", "")),
            ),
        )
    )
    expected_mutations = _expected_mutations(action)
    stable_payload = {
        "dataset_id": descriptor.dataset_id,
        "version": descriptor.version,
        "dataset_hash": descriptor.dataset_hash,
        "selected_data_type": data_type,
        "action": action,
        "affected_ranges": affected_ranges,
        "evidence": ordered_evidence,
        "expected_mutations": expected_mutations,
        "requires_network": requires_network,
    }
    preview_id = f"repair-preview:{sha256_canonical(stable_payload)[:24]}"
    return RepairPlanPreview(
        dataset_id=descriptor.dataset_id,
        version=descriptor.version,
        preview_id=preview_id,
        action=action,
        selected_data_type=data_type,
        checked_partitions=len(candidates),
        damaged_partitions=len(damaged),
        unresolved_gaps=sum(len(item[2]) for item in unknown_gap_entries),
        affected_ranges=affected_ranges,
        evidence=ordered_evidence,
        expected_mutations=expected_mutations,
        confirmation_required=action != "no_action",
        requires_network=requires_network,
    )


def _classify_action(
    damaged: list[NormalizedPartitionDescriptor],
    gap_entries: tuple[
        tuple[DataType, str | None, tuple[object, ...]],
        ...,
    ],
) -> tuple[RepairAction, bool]:
    if damaged:
        damaged_types = {item.data_type for item in damaged}
        if len(damaged_types) > 1:
            return "multi_stream_staged_repair", True
        normalized = tuple(item for item in damaged if item.storage_uri.startswith("normalized/"))
        derived = tuple(item for item in damaged if item.storage_uri.startswith("derived/"))
        metadata = tuple(item for item in normalized if item.data_type in _METADATA_TYPES)
        series = tuple(item for item in normalized if item.data_type not in _METADATA_TYPES)
        if metadata and not series and not derived:
            return "metadata_local_rebuild", False
        if derived and not normalized:
            return "derived_local_rebuild", False
        streams = {(item.data_type, item.timeframe) for item in series}
        if len(streams) == 1:
            return "stream_redownload_and_rebuild", True
        return "separate_repairs_required", True

    if gap_entries:
        if len(gap_entries) == 1:
            gap_type, timeframe, _ = gap_entries[0]
            if gap_type in _INTERVAL_GAP_TYPES and timeframe is not None:
                return "gap_range_repair", True
        return "separate_repairs_required", True
    return "no_action", False


def _expected_mutations(action: RepairAction) -> tuple[str, ...]:
    if action == "no_action":
        return ()
    common = (
        "repair_audit_write",
        "manifest_write",
        "dataset_registry_register",
    )
    if action == "derived_local_rebuild":
        return ("derived_partition_write", *common)
    if action == "metadata_local_rebuild":
        return ("normalized_partition_write", *common)
    if action in {"gap_range_repair", "stream_redownload_and_rebuild"}:
        return (
            "raw_artifact_write",
            "normalized_partition_write",
            "dependent_derived_partition_write",
            *common,
        )
    return (
        "one_or_more_immutable_dataset_versions",
        "partition_write",
        *common,
    )


def _layer(partition: NormalizedPartitionDescriptor) -> str:
    return "derived" if partition.storage_uri.startswith("derived/") else "normalized"
