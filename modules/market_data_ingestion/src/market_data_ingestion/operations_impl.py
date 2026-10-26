from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Iterator
from dataclasses import replace
from datetime import UTC, datetime
from itertools import chain, groupby
from pathlib import Path

from .aggregation import aggregate_klines_stream
from .canonical import sha256_file
from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    DataTypeValidationReport,
    DataValidationReport,
    GapDescriptor,
    IngestionRunResult,
    InstrumentReference,
    MarketDataRequest,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
    RepairResult,
)
from .enums import DataType, GapReason, RepairStatus, Severity, ValidationStatus
from .errors import RegistryError, StructuredIngestionError
from .hashing import dataset_hash
from .integrity import RegisteredPathError, resolve_registered_path
from .manifest_io import load_manifest
from .pipeline import IngestionService
from .records import (
    ExchangeMetadataRecord,
    InstrumentMetadataRecord,
    KlineRecord,
    MarketDataRecord,
    NormalizedRecord,
)
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .spool import RecordSpool
from .storage import LocalStorage
from .timeframes import timeframe_delta, timeframe_seconds
from .validation import validate_sorted_records

_INTERVAL_GAP_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)

_SERIES_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)


class DatasetOperations:
    """Version-preserving update and backfill operations over immutable datasets."""

    def __init__(
        self,
        *,
        service: IngestionService,
        storage: LocalStorage,
        registry: SQLiteDatasetRegistry,
    ) -> None:
        self._service = service
        self._storage = storage
        self._registry = registry

    def update(
        self,
        dataset_id: str,
        *,
        end: datetime,
        version: int | None = None,
        request_id: str | None = None,
        required_data: Iterable[DataType] | None = None,
        format_name: str = "parquet",
        compression: str = "zstd",
    ) -> IngestionRunResult:
        parent = self._descriptor(dataset_id, version)
        manifest = load_manifest(self._storage.root, parent)
        update_end = self._utc(end, "update end")
        if update_end <= parent.end:
            raise StructuredIngestionError(
                "UPDATE_RANGE_EMPTY",
                "update end must be after the registered dataset end",
                False,
                "planning",
            )
        base_timeframe = self._base_timeframe(manifest.partitions)
        available = tuple(item for item in parent.data_types if item in _SERIES_TYPES)
        explicit_required = tuple(required_data or ())
        if explicit_required:
            unknown = set(explicit_required) - set(available)
            if unknown:
                raise StructuredIngestionError(
                    "UPDATE_DATA_TYPE_UNKNOWN",
                    "required update types are not present in the base dataset",
                    False,
                    "planning",
                    {"unknown": sorted(item.value for item in unknown)},
                )
            required = explicit_required
        elif DataType.KLINES in available:
            required = (DataType.KLINES,)
        elif available:
            required = (available[0],)
        else:
            raise StructuredIngestionError(
                "UPDATE_NO_SERIES",
                "base dataset contains no updateable historical stream",
                False,
                "planning",
            )
        optional = tuple(item for item in available if item not in required)
        derived = tuple(
            item
            for item in parent.timeframes
            if item != base_timeframe
            and timeframe_seconds(item) % timeframe_seconds(base_timeframe) == 0
        )
        request = MarketDataRequest(
            request_id=request_id or self._request_id("update", parent, update_end, required),
            exchange_id=parent.exchange_id,
            market_type=parent.market_type,
            symbol=manifest.instrument.symbol,
            required_data=required,
            optional_data=optional,
            base_timeframe=base_timeframe,
            derived_timeframes=derived,
            start=parent.end,
            end=update_end,
        )
        return self._service.execute(
            request,
            format_name=format_name,
            compression=compression,
            base_dataset=parent,
        )

    def backfill(
        self,
        dataset_id: str,
        *,
        data_type: DataType,
        version: int | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        request_id: str | None = None,
        format_name: str = "parquet",
        compression: str = "zstd",
    ) -> IngestionRunResult:
        if data_type not in _SERIES_TYPES:
            raise StructuredIngestionError(
                "BACKFILL_TYPE_UNSUPPORTED",
                "backfill currently supports historical series data types only",
                False,
                "planning",
            )
        parent = self._descriptor(dataset_id, version)
        manifest = load_manifest(self._storage.root, parent)
        base_timeframe = self._base_timeframe(manifest.partitions)
        range_start = self._utc(start or parent.start, "backfill start")
        range_end = self._utc(end or parent.end, "backfill end")
        if range_start >= range_end:
            raise StructuredIngestionError(
                "BACKFILL_RANGE_INVALID",
                "backfill start must precede end",
                False,
                "planning",
            )
        existing = [
            item
            for item in manifest.partitions
            if item.data_type is data_type
            and item.timeframe == (None if data_type is DataType.FUNDING_RATE else base_timeframe)
        ]
        for partition in existing:
            if range_start < partition.end and partition.start < range_end:
                raise StructuredIngestionError(
                    "BACKFILL_RANGE_OVERLAP",
                    "backfill range overlaps an already registered stream; "
                    "use repair for replacement",
                    False,
                    "planning",
                    {"partition_id": partition.partition_id},
                )
        request = MarketDataRequest(
            request_id=request_id or self._request_id("backfill", parent, range_end, (data_type,)),
            exchange_id=parent.exchange_id,
            market_type=parent.market_type,
            symbol=manifest.instrument.symbol,
            required_data=(data_type,),
            optional_data=(),
            base_timeframe=base_timeframe,
            derived_timeframes=(),
            start=range_start,
            end=range_end,
        )
        return self._service.execute(
            request,
            format_name=format_name,
            compression=compression,
            base_dataset=parent,
        )

    def repair(
        self,
        dataset_id: str,
        *,
        version: int | None = None,
        data_type: DataType | None = None,
        request_id: str | None = None,
        format_name: str = "parquet",
        compression: str = "zstd",
    ) -> RepairResult:
        parent = self._descriptor(dataset_id, version)
        manifest = load_manifest(self._storage.root, parent)
        candidates = [
            item
            for item in manifest.partitions
            if (
                item.storage_uri.startswith("normalized/")
                or item.storage_uri.startswith("derived/")
            )
            and (data_type is None or item.data_type is data_type)
        ]
        damaged: list[NormalizedPartitionDescriptor] = []
        evidence: list[dict[str, object]] = []
        root = self._storage.root.resolve()
        for partition in candidates:
            issue = self._partition_integrity_issue(root, partition)
            if issue is not None:
                damaged.append(partition)
                evidence.append(issue)

        audit_name = (
            f"repair-{parent.dataset_id}-v{parent.version}-"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        gap_reports = tuple(
            item
            for item in manifest.data_type_reports
            if item.report.gaps and (data_type is None or item.data_type is data_type)
        )
        if not damaged and gap_reports:
            if (
                len(gap_reports) == 1
                and gap_reports[0].data_type in _INTERVAL_GAP_TYPES
                and gap_reports[0].timeframe is not None
            ):
                return self._repair_interval_gaps(
                    parent=parent,
                    manifest=manifest,
                    report_entry=gap_reports[0],
                    audit_name=audit_name,
                    format_name=format_name,
                    compression=compression,
                    request_id=request_id,
                )
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "gap_stream_requires_separate_repair",
                    "gap_streams": [
                        {
                            "data_type": item.data_type.value,
                            "timeframe": item.timeframe,
                            "gaps": len(item.report.gaps),
                        }
                        for item in gap_reports
                    ],
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.PARTIALLY_REPAIRED,
                repaired_ranges=(),
                unresolved_gaps=tuple(manifest.validation.gaps),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        if not damaged:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "no_damage_detected",
                    "checked_partitions": len(candidates),
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.REPAIRED,
                repaired_ranges=(),
                unresolved_gaps=(),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        damaged_normalized = [
            item for item in damaged if item.storage_uri.startswith("normalized/")
        ]
        damaged_metadata = [
            item
            for item in damaged_normalized
            if item.data_type in {DataType.INSTRUMENT_METADATA, DataType.EXCHANGE_METADATA}
        ]
        damaged_series = [
            item
            for item in damaged_normalized
            if item.data_type not in {DataType.INSTRUMENT_METADATA, DataType.EXCHANGE_METADATA}
        ]
        damaged_derived = [item for item in damaged if item.storage_uri.startswith("derived/")]
        damaged_types = tuple(
            sorted(
                {item.data_type for item in damaged},
                key=lambda item: (
                    0
                    if item
                    in {
                        DataType.INSTRUMENT_METADATA,
                        DataType.EXCHANGE_METADATA,
                    }
                    else 1,
                    item.value,
                ),
            )
        )
        if len(damaged_types) > 1:
            return self._repair_multiple_data_types(
                parent=parent,
                damaged_types=damaged_types,
                audit_name=audit_name,
                request_id=request_id,
                format_name=format_name,
                compression=compression,
            )
        if damaged_metadata and not damaged_series and not damaged_derived:
            return self._rebuild_metadata_partitions(
                parent=parent,
                manifest=manifest,
                damaged=tuple(damaged_metadata),
                evidence=tuple(evidence),
                audit_name=audit_name,
                format_name=format_name,
                compression=compression,
            )
        if damaged_derived and not damaged_series and not damaged_metadata:
            return self._rebuild_derived_partitions(
                parent=parent,
                manifest=manifest,
                damaged=tuple(damaged_derived),
                evidence=tuple(evidence),
                audit_name=audit_name,
                format_name=format_name,
                compression=compression,
            )

        streams = {(item.data_type, item.timeframe) for item in damaged_series}
        if len(streams) != 1:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "multiple_streams_require_separate_repairs",
                    "evidence": evidence,
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.PARTIALLY_REPAIRED,
                repaired_ranges=(),
                unresolved_gaps=tuple(manifest.validation.gaps),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        stream_type, stream_timeframe = next(iter(streams))
        if stream_type not in _SERIES_TYPES:
            raise StructuredIngestionError(
                "REPAIR_TYPE_UNSUPPORTED",
                "damaged stream type is not supported by the repair engine",
                False,
                "repair",
            )
        base_timeframe = self._base_timeframe(manifest.partitions)
        if stream_type is not DataType.FUNDING_RATE and stream_timeframe != base_timeframe:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "derived_partition_requires_local_rebuild",
                    "evidence": evidence,
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.UNRESOLVED,
                repaired_ranges=(),
                unresolved_gaps=tuple(manifest.validation.gaps),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        damaged_ids = {item.partition_id for item in damaged_series}
        dependent_derived = {
            item.partition_id
            for item in manifest.partitions
            if item.storage_uri.startswith("derived/")
            and damaged_ids.intersection(item.source_artifact_ids)
        }
        removed_ids = damaged_ids | dependent_derived
        repair_start = min(item.start for item in damaged_series)
        repair_end = max(item.end for item in damaged_series)
        retained_partitions = tuple(
            item for item in manifest.partitions if item.partition_id not in removed_ids
        )
        pruned = replace(
            manifest,
            partitions=retained_partitions,
            data_types=tuple(
                sorted(
                    {item.data_type for item in retained_partitions},
                    key=lambda item: item.value,
                )
            ),
            timeframes=tuple(
                sorted(
                    {item.timeframe for item in retained_partitions if item.timeframe is not None},
                    key=lambda item: (timeframe_seconds(item), item),
                )
            ),
        )
        derived = tuple(
            sorted(
                {
                    item.timeframe
                    for item in manifest.partitions
                    if item.partition_id in dependent_derived and item.timeframe is not None
                },
                key=lambda item: (timeframe_seconds(item), item),
            )
        )
        request = MarketDataRequest(
            request_id=request_id or self._request_id("repair", parent, repair_end, (stream_type,)),
            exchange_id=parent.exchange_id,
            market_type=parent.market_type,
            symbol=manifest.instrument.symbol,
            required_data=(stream_type,),
            optional_data=(),
            base_timeframe=base_timeframe,
            derived_timeframes=derived if stream_type is DataType.KLINES else (),
            start=repair_start,
            end=repair_end,
        )
        try:
            result = self._service.execute(
                request,
                format_name=format_name,
                compression=compression,
                base_dataset=parent,
                base_manifest_override=pruned,
            )
        except StructuredIngestionError as exc:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "source_unavailable" if exc.retryable else "unresolved",
                    "evidence": evidence,
                    "error": exc.to_dict(),
                    "removed_partition_ids": sorted(removed_ids),
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=(
                    RepairStatus.SOURCE_UNAVAILABLE if exc.retryable else RepairStatus.UNRESOLVED
                ),
                repaired_ranges=(),
                unresolved_gaps=tuple(manifest.validation.gaps),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        if result.dataset is None:
            status = RepairStatus.UNRESOLVED
            new_version = None
            unresolved = tuple(result.validation.gaps) if result.validation is not None else ()
            repaired_ranges: tuple[tuple[datetime, datetime], ...] = ()
        else:
            status = (
                RepairStatus.REPAIRED
                if not result.dataset.unresolved_issues
                else RepairStatus.PARTIALLY_REPAIRED
            )
            new_version = result.dataset.version
            unresolved = tuple(result.validation.gaps) if result.validation is not None else ()
            repaired_ranges = ((repair_start, repair_end),)
        audit_path = self._storage.write_repair_audit(
            audit_name,
            {
                "dataset_id": parent.dataset_id,
                "parent_version": parent.version,
                "new_version": new_version,
                "status": status.value,
                "data_type": stream_type.value,
                "timeframe": stream_timeframe,
                "repair_start": repair_start,
                "repair_end": repair_end,
                "removed_partition_ids": sorted(removed_ids),
                "evidence": evidence,
                "new_dataset_hash": (
                    result.dataset.dataset_hash if result.dataset is not None else None
                ),
                "created_at": datetime.now(UTC),
            },
        )
        return RepairResult(
            status=status,
            repaired_ranges=repaired_ranges,
            unresolved_gaps=unresolved,
            new_dataset_version=new_version,
            audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
        )

    def _repair_multiple_data_types(
        self,
        *,
        parent: DatasetDescriptor,
        damaged_types: tuple[DataType, ...],
        audit_name: str,
        request_id: str | None,
        format_name: str,
        compression: str,
    ) -> RepairResult:
        """Repair independent damaged streams through immutable staged versions."""

        current_version = parent.version
        child_audits: list[str] = []
        repaired_ranges: list[tuple[datetime, datetime]] = []
        successful_stages = 0
        terminal_failure: RepairStatus | None = None
        for index, stream_type in enumerate(damaged_types, start=1):
            child = self.repair(
                parent.dataset_id,
                version=current_version,
                data_type=stream_type,
                request_id=(f"{request_id or audit_name}-{index:02d}-{stream_type.value}"),
                format_name=format_name,
                compression=compression,
            )
            child_audits.append(child.audit_uri)
            repaired_ranges.extend(child.repaired_ranges)
            if child.new_dataset_version is not None:
                current_version = child.new_dataset_version
                successful_stages += 1
            if child.status not in {
                RepairStatus.REPAIRED,
                RepairStatus.PARTIALLY_REPAIRED,
            }:
                terminal_failure = child.status
                break
            if child.status is RepairStatus.PARTIALLY_REPAIRED:
                terminal_failure = RepairStatus.PARTIALLY_REPAIRED
                break

        current_payload = self._registry.inspect(parent.dataset_id, current_version)
        if current_payload is None:
            raise RegistryError(
                "DATASET_NOT_FOUND",
                "staged repair dataset disappeared from the registry",
                False,
                "repair",
            )
        current = descriptor_from_dict(current_payload)
        current_manifest = load_manifest(self._storage.root, current)
        unresolved = tuple(current_manifest.validation.gaps)
        if terminal_failure is None:
            status = RepairStatus.REPAIRED
        elif successful_stages:
            status = RepairStatus.PARTIALLY_REPAIRED
        else:
            status = terminal_failure
        audit_path = self._storage.write_repair_audit(
            audit_name,
            {
                "dataset_id": parent.dataset_id,
                "parent_version": parent.version,
                "new_version": (current_version if current_version != parent.version else None),
                "status": status.value,
                "mode": "multi_stream_staged_repair",
                "damaged_data_types": [item.value for item in damaged_types],
                "child_audits": child_audits,
                "completed_stages": successful_stages,
                "created_at": datetime.now(UTC),
            },
        )
        return RepairResult(
            status=status,
            repaired_ranges=tuple(dict.fromkeys(repaired_ranges)),
            unresolved_gaps=unresolved,
            new_dataset_version=(current_version if current_version != parent.version else None),
            audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
        )

    @staticmethod
    def _partition_integrity_issue(
        root: Path, partition: NormalizedPartitionDescriptor
    ) -> dict[str, object] | None:
        try:
            path = resolve_registered_path(root, partition.storage_uri, kind="partition")
        except RegisteredPathError:
            return {"partition_id": partition.partition_id, "issue": "unsafe_path"}
        if not path.is_file():
            return {"partition_id": partition.partition_id, "issue": "missing"}
        actual = sha256_file(path)
        if actual != partition.checksum_sha256:
            return {
                "partition_id": partition.partition_id,
                "issue": "checksum_mismatch",
                "expected": partition.checksum_sha256,
                "actual": actual,
            }
        return None

    def _repair_interval_gaps(
        self,
        *,
        parent: DatasetDescriptor,
        manifest: DatasetManifest,
        report_entry: DataTypeValidationReport,
        audit_name: str,
        format_name: str,
        compression: str,
        request_id: str | None,
    ) -> RepairResult:
        """Repair fixed-interval gaps without rewriting unrelated streams."""

        data_type = report_entry.data_type
        timeframe = report_entry.timeframe
        if data_type not in _INTERVAL_GAP_TYPES:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, (), "gap_data_type_unsupported"
            )
        if timeframe is None:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, (), "gap_timeframe_missing"
            )
        gaps = tuple(gap for gap in report_entry.report.gaps if gap.reason is GapReason.UNKNOWN)
        if not gaps:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "no_unresolved_gaps",
                    "data_type": data_type.value,
                    "timeframe": timeframe,
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.REPAIRED,
                repaired_ranges=(),
                unresolved_gaps=tuple(manifest.validation.gaps),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        adapter = self._service.adapter(parent.exchange_id)
        if adapter.adapter_version != manifest.exchange.adapter_version:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, (), "gap_adapter_version_mismatch"
            )
        impacted = tuple(
            item
            for item in manifest.partitions
            if item.storage_uri.startswith("normalized/")
            and item.data_type is data_type
            and item.timeframe == timeframe
            and any(item.start < gap.end and gap.start < item.end for gap in gaps)
        )
        if not impacted:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, (), "gap_partition_not_found"
            )
        for partition in impacted:
            issue = self._partition_integrity_issue(self._storage.root.resolve(), partition)
            if issue is not None:
                return self._unresolved_local_rebuild(
                    parent, manifest, audit_name, (issue,), "gap_partition_damaged"
                )

        expected_times: set[datetime] = set()
        step = timeframe_delta(timeframe)
        for gap in gaps:
            expected_times.update(
                gap.start + index * step for index in range(gap.missing_intervals)
            )
        recovered_by_time: dict[datetime, NormalizedRecord] = {}
        raw_artifacts: list[RawArtifactDescriptor] = []
        source_conflicts: list[dict[str, object]] = []
        repair_identity = request_id or (
            f"gap-{parent.dataset_id}-v{parent.version}-"
            f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}"
        )
        try:
            for gap_index, gap in enumerate(gaps, start=1):
                request = MarketDataRequest(
                    request_id=f"{repair_identity}-{gap_index:04d}",
                    exchange_id=parent.exchange_id,
                    market_type=parent.market_type,
                    symbol=manifest.instrument.symbol,
                    required_data=(data_type,),
                    optional_data=(),
                    base_timeframe=timeframe,
                    derived_timeframes=(),
                    start=gap.start,
                    end=gap.end,
                )
                plan = adapter.plan_download(request, manifest.instrument)
                if not plan.chunks:
                    source_conflicts.append(
                        {
                            "gap_start": gap.start,
                            "gap_end": gap.end,
                            "issue": "empty_download_plan",
                        }
                    )
                    continue
                for chunk in plan.chunks:
                    if (
                        chunk.data_type is not data_type
                        or chunk.timeframe != timeframe
                        or chunk.start < gap.start
                        or chunk.end > gap.end
                    ):
                        source_conflicts.append(
                            {
                                "chunk_id": chunk.chunk_id,
                                "issue": "download_plan_scope_mismatch",
                                "data_type": chunk.data_type.value,
                                "timeframe": chunk.timeframe,
                                "start": chunk.start,
                                "end": chunk.end,
                            }
                        )
                        continue
                    acquired = adapter.acquire(chunk, manifest.instrument)
                    raw = self._storage.write_raw(
                        run_id=repair_identity,
                        instrument=manifest.instrument,
                        chunk=chunk,
                        payload=acquired.raw_payload,
                        source_uri=acquired.source_uri,
                        adapter_version=adapter.adapter_version,
                        response_metadata=acquired.response_metadata,
                        upstream_checksum=acquired.upstream_checksum,
                        source_kind=acquired.chunk.source,
                    )
                    raw_artifacts.append(raw)
                    for record in acquired.records:
                        if not isinstance(record, MarketDataRecord):
                            source_conflicts.append(
                                {
                                    "chunk_id": chunk.chunk_id,
                                    "issue": "record_contract_mismatch",
                                }
                            )
                            continue
                        if (
                            record.data_type is not data_type
                            or record.record_timeframe != timeframe
                        ):
                            source_conflicts.append(
                                {
                                    "chunk_id": chunk.chunk_id,
                                    "issue": "record_stream_mismatch",
                                    "observed_data_type": record.data_type.value,
                                    "observed_timeframe": record.record_timeframe,
                                }
                            )
                            continue
                        previous = recovered_by_time.get(record.timestamp)
                        if previous is not None and previous.to_row() != record.to_row():
                            source_conflicts.append(
                                {
                                    "chunk_id": chunk.chunk_id,
                                    "issue": "conflicting_recovered_timestamp",
                                    "timestamp": record.timestamp,
                                }
                            )
                            continue
                        recovered_by_time[record.timestamp] = record
        except StructuredIngestionError as exc:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "source_unavailable" if exc.retryable else "unresolved",
                    "mode": "gap_range_repair",
                    "data_type": data_type.value,
                    "timeframe": timeframe,
                    "error": exc.to_dict(),
                    "gaps": gaps,
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=(
                    RepairStatus.SOURCE_UNAVAILABLE if exc.retryable else RepairStatus.UNRESOLVED
                ),
                repaired_ranges=(),
                unresolved_gaps=gaps,
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )
        except (TypeError, ValueError) as exc:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "source_contract_violation",
                    "mode": "gap_range_repair",
                    "data_type": data_type.value,
                    "timeframe": timeframe,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc),
                    "gaps": gaps,
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.SOURCE_CONFLICT,
                repaired_ranges=(),
                unresolved_gaps=gaps,
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        observed_times = set(recovered_by_time)
        if source_conflicts or observed_times != expected_times:
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "source_range_mismatch",
                    "mode": "gap_range_repair",
                    "data_type": data_type.value,
                    "timeframe": timeframe,
                    "expected_timestamps": sorted(expected_times),
                    "observed_timestamps": sorted(observed_times),
                    "source_conflicts": source_conflicts,
                    "raw_artifact_ids": [item.artifact_id for item in raw_artifacts],
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.SOURCE_CONFLICT,
                repaired_ranges=(),
                unresolved_gaps=gaps,
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        spool_path = self._storage.root / "staging" / f"{audit_name}-gap-merge.sqlite3"
        with RecordSpool(spool_path, reset=True) as spool:
            for partition in impacted:
                spool.append("acquired", self._storage.iter_partition_records(partition))
            spool.append("acquired", recovered_by_time.values())
            accepted_batch: list[NormalizedRecord] = []

            def accept_record(record: NormalizedRecord) -> None:
                if record.data_type is not data_type or record.record_timeframe != timeframe:
                    raise TypeError("gap repair accepted a record from another stream")
                accepted_batch.append(record)
                if len(accepted_batch) >= 1_000:
                    spool.append("validated", accepted_batch)
                    accepted_batch.clear()

            merged_report = validate_sorted_records(
                spool.iter_sorted("acquired", data_type, timeframe),
                data_type=data_type,
                instrument=manifest.instrument,
                timeframe=timeframe,
                accept=accept_record,
                expected_start=min(item.start for item in impacted),
                expected_end=max(item.end for item in impacted),
            )
            if accepted_batch:
                spool.append("validated", accepted_batch)
            unresolved_after_merge = tuple(
                gap for gap in merged_report.gaps if gap.reason is GapReason.UNKNOWN
            )
            if unresolved_after_merge or merged_report.status in {
                ValidationStatus.INVALID,
                ValidationStatus.REJECTED,
            }:
                audit_path = self._storage.write_repair_audit(
                    audit_name,
                    {
                        "dataset_id": parent.dataset_id,
                        "version": parent.version,
                        "status": "merged_stream_invalid",
                        "mode": "gap_range_repair",
                        "data_type": data_type.value,
                        "timeframe": timeframe,
                        "validation": merged_report,
                        "created_at": datetime.now(UTC),
                    },
                )
                return RepairResult(
                    status=RepairStatus.SOURCE_CONFLICT,
                    repaired_ranges=(),
                    unresolved_gaps=gaps,
                    new_dataset_version=None,
                    audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
                )

            version = self._registry.next_version(parent.dataset_id)
            source_ids = tuple(
                dict.fromkeys(
                    (
                        *(source_id for item in impacted for source_id in item.source_artifact_ids),
                        *(item.artifact_id for item in raw_artifacts),
                    )
                )
            )
            replacements: list[NormalizedPartitionDescriptor] = []
            for _, group in groupby(
                spool.iter_sorted("validated", data_type, timeframe),
                key=lambda item: (item.timestamp.year, item.timestamp.month),
            ):
                replacements.append(
                    self._storage.write_partition(
                        dataset_id=parent.dataset_id,
                        version=version,
                        layer="normalized",
                        instrument=manifest.instrument,
                        data_type=data_type,
                        timeframe=timeframe,
                        records=group,
                        source_artifact_ids=source_ids,
                        format_name=format_name,
                        compression=compression,
                    )
                )

        impacted_ids = {item.partition_id for item in impacted}
        dependent = tuple(
            item
            for item in manifest.partitions
            if item.storage_uri.startswith("derived/")
            and impacted_ids.intersection(item.source_artifact_ids)
        )
        if dependent and data_type is not DataType.KLINES:
            return self._unresolved_local_rebuild(
                parent,
                manifest,
                audit_name,
                (),
                "auxiliary_stream_has_unexpected_derived_dependents",
            )
        dependent_ids = {item.partition_id for item in dependent}
        derived_replacements: list[NormalizedPartitionDescriptor] = []
        if data_type is DataType.KLINES:
            normalized_after = {
                item.partition_id: item
                for item in (
                    *(
                        item
                        for item in manifest.partitions
                        if item.storage_uri.startswith("normalized/")
                        and item.data_type is DataType.KLINES
                        and item.partition_id not in impacted_ids
                    ),
                    *replacements,
                )
            }
            replacement_by_old: dict[str, tuple[NormalizedPartitionDescriptor, ...]] = {}
            for old_partition in impacted:
                replacement_by_old[old_partition.partition_id] = tuple(
                    item
                    for item in replacements
                    if item.start < old_partition.end and old_partition.start < item.end
                )
            for partition in dependent:
                if partition.timeframe is None:
                    continue
                translated_source_ids: list[str] = []
                for source_id in partition.source_artifact_ids:
                    replacements_for_source = replacement_by_old.get(source_id)
                    if replacements_for_source is not None:
                        translated_source_ids.extend(
                            item.partition_id for item in replacements_for_source
                        )
                    else:
                        translated_source_ids.append(source_id)
                translated_source_ids = list(dict.fromkeys(translated_source_ids))
                source_partitions: list[NormalizedPartitionDescriptor] = []
                for source_id in translated_source_ids:
                    source_partition = normalized_after.get(source_id)
                    if source_partition is None:
                        return self._unresolved_local_rebuild(
                            parent,
                            manifest,
                            audit_name,
                            (),
                            "gap_derived_lineage_missing",
                        )
                    issue = self._partition_integrity_issue(
                        self._storage.root.resolve(), source_partition
                    )
                    if issue is not None:
                        return self._unresolved_local_rebuild(
                            parent,
                            manifest,
                            audit_name,
                            (issue,),
                            "gap_derived_source_damaged",
                        )
                    source_partitions.append(source_partition)
                rebuilt = self._write_derived_range(
                    dataset_id=parent.dataset_id,
                    version=version,
                    instrument=manifest.instrument,
                    target_timeframe=partition.timeframe,
                    start=partition.start,
                    end=partition.end,
                    source_partitions=source_partitions,
                    source_ids=tuple(translated_source_ids),
                    format_name=format_name,
                    compression=compression,
                )
                if rebuilt is None:
                    return self._unresolved_local_rebuild(
                        parent,
                        manifest,
                        audit_name,
                        (),
                        "gap_derived_rebuild_empty",
                    )
                derived_replacements.append(rebuilt)

        removed_ids = impacted_ids | dependent_ids
        partitions = tuple(
            sorted(
                (
                    *(item for item in manifest.partitions if item.partition_id not in removed_ids),
                    *replacements,
                    *derived_replacements,
                ),
                key=lambda item: (
                    item.data_type.value,
                    item.timeframe or "",
                    item.start,
                    item.end,
                    item.partition_id,
                ),
            )
        )
        clean_report = self._report_without_repaired_gaps(report_entry.report, gaps)
        reports = tuple(
            DataTypeValidationReport(
                data_type=item.data_type,
                timeframe=item.timeframe,
                report=(
                    clean_report
                    if item.data_type is data_type and item.timeframe == timeframe
                    else item.report
                ),
            )
            for item in manifest.data_type_reports
        )
        full_validation = self._report_without_repaired_gaps(manifest.validation, gaps)
        rebuilt_manifest = replace(
            manifest,
            version=version,
            partitions=partitions,
            source_artifacts=tuple(
                {
                    item.artifact_id: item for item in (*manifest.source_artifacts, *raw_artifacts)
                }.values()
            ),
            validation=full_validation,
            data_type_reports=reports,
            lineage=(
                *manifest.lineage,
                f"parent:{parent.dataset_id}:v{parent.version}:{parent.dataset_hash}",
                f"repair:gap-ranges:{data_type.value}:{timeframe}",
                *(f"raw:{item.checksum_sha256}" for item in raw_artifacts),
            ),
            created_at=datetime.now(UTC),
        )
        digest = dataset_hash(rebuilt_manifest)
        manifest_path = self._storage.write_manifest(parent.dataset_id, version, rebuilt_manifest)
        unresolved_codes = tuple(dict.fromkeys(issue.code for issue in full_validation.issues))
        descriptor = DatasetDescriptor(
            dataset_id=parent.dataset_id,
            version=version,
            dataset_hash=digest,
            manifest_uri=manifest_path.relative_to(self._storage.root).as_posix(),
            storage_uri=self._storage.root.as_uri(),
            exchange_id=parent.exchange_id,
            market_type=parent.market_type,
            instrument_id=parent.instrument_id,
            data_types=rebuilt_manifest.data_types,
            timeframes=rebuilt_manifest.timeframes,
            start=rebuilt_manifest.start,
            end=rebuilt_manifest.end,
            schema_version=rebuilt_manifest.schema_version,
            validation_status=full_validation.status,
            unresolved_issues=unresolved_codes,
            lineage=rebuilt_manifest.lineage,
            compatible_consumers=parent.compatible_consumers,
        )
        self._registry.register(descriptor)
        audit_path = self._storage.write_repair_audit(
            audit_name,
            {
                "dataset_id": parent.dataset_id,
                "parent_version": parent.version,
                "new_version": version,
                "status": RepairStatus.REPAIRED.value,
                "mode": "gap_range_repair",
                "data_type": data_type.value,
                "timeframe": timeframe,
                "repaired_gaps": gaps,
                "removed_partition_ids": sorted(removed_ids),
                "replacement_partition_ids": sorted(
                    item.partition_id for item in (*replacements, *derived_replacements)
                ),
                "raw_artifact_ids": sorted(item.artifact_id for item in raw_artifacts),
                "new_dataset_hash": digest,
                "created_at": datetime.now(UTC),
            },
        )
        return RepairResult(
            status=RepairStatus.REPAIRED,
            repaired_ranges=tuple((gap.start, gap.end) for gap in gaps),
            unresolved_gaps=tuple(full_validation.gaps),
            new_dataset_version=version,
            audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
        )

    @staticmethod
    def _report_without_repaired_gaps(
        report: DataValidationReport, gaps: tuple[GapDescriptor, ...]
    ) -> DataValidationReport:
        repaired = {(item.start, item.end, item.timeframe) for item in gaps}
        remaining_gaps = tuple(
            item for item in report.gaps if (item.start, item.end, item.timeframe) not in repaired
        )
        issues = tuple(
            issue
            for issue in report.issues
            if not (
                issue.code == "unresolved_gap"
                and issue.start is not None
                and issue.end is not None
                and any(issue.start == start and issue.end == end for start, end, _ in repaired)
            )
        )
        severities = {issue.severity for issue in issues}
        if Severity.CRITICAL in severities:
            status = ValidationStatus.REJECTED
        elif Severity.ERROR in severities:
            status = ValidationStatus.INVALID
        elif Severity.WARNING in severities or report.duplicates:
            status = ValidationStatus.VALID_WITH_WARNINGS
        else:
            status = ValidationStatus.VALID
        return DataValidationReport(
            status=status,
            issues=issues,
            gaps=remaining_gaps,
            duplicates=report.duplicates,
            rows_checked=report.rows_checked + sum(item.missing_intervals for item in gaps),
            created_at=datetime.now(UTC),
            report_version=report.report_version,
        )

    def _rebuild_metadata_partitions(
        self,
        *,
        parent: DatasetDescriptor,
        manifest: DatasetManifest,
        damaged: tuple[NormalizedPartitionDescriptor, ...],
        evidence: tuple[dict[str, object], ...],
        audit_name: str,
        format_name: str,
        compression: str,
    ) -> RepairResult:
        adapter = self._service.adapter(parent.exchange_id)
        if adapter.adapter_version != manifest.exchange.adapter_version:
            return self._unresolved_local_rebuild(
                parent,
                manifest,
                audit_name,
                evidence,
                "metadata_adapter_version_mismatch",
            )
        source_ids = tuple(
            dict.fromkeys(source_id for item in damaged for source_id in item.source_artifact_ids)
        )
        if len(source_ids) != 1:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, evidence, "metadata_raw_lineage_ambiguous"
            )
        found = self._storage.find_raw_artifact(source_ids[0])
        if found is None:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, evidence, "metadata_raw_evidence_missing"
            )
        raw_payload, raw_descriptor = found
        if raw_descriptor.adapter_version != adapter.adapter_version:
            return self._unresolved_local_rebuild(
                parent,
                manifest,
                audit_name,
                evidence,
                "metadata_raw_adapter_version_mismatch",
            )
        resolution = adapter.restore_instrument_resolution(
            parent.market_type, manifest.instrument.symbol, raw_payload
        )
        metadata = resolution.metadata
        if metadata.reference != manifest.instrument:
            return self._unresolved_local_rebuild(
                parent, manifest, audit_name, evidence, "metadata_identity_mismatch"
            )
        capabilities = adapter.get_capabilities()
        version = self._registry.next_version(parent.dataset_id)
        replacements: list[NormalizedPartitionDescriptor] = []
        for partition in damaged:
            effective_at = partition.start
            record: NormalizedRecord
            if partition.data_type is DataType.INSTRUMENT_METADATA:
                record = InstrumentMetadataRecord(
                    effective_at=effective_at,
                    symbol=metadata.reference.symbol,
                    exchange_id=metadata.reference.exchange_id,
                    market_type=metadata.reference.market_type.value,
                    source_kind=raw_descriptor.source.value,
                    canonical_id=metadata.reference.canonical_id,
                    base_asset=metadata.base_asset,
                    quote_asset=metadata.quote_asset,
                    settlement_asset=metadata.settlement_asset,
                    contract_type=metadata.contract_type,
                    listing_date=metadata.listing_date,
                    delisting_date=metadata.delisting_date,
                    tick_size=metadata.tick_size,
                    quantity_step=metadata.quantity_step,
                    contract_multiplier=metadata.contract_multiplier,
                    trading_status=metadata.trading_status,
                )
            elif partition.data_type is DataType.EXCHANGE_METADATA:
                record = ExchangeMetadataRecord(
                    effective_at=effective_at,
                    symbol=metadata.reference.symbol,
                    exchange_id=metadata.reference.exchange_id,
                    market_type=metadata.reference.market_type.value,
                    source_kind=raw_descriptor.source.value,
                    adapter_version=adapter.adapter_version,
                    maturity=capabilities.maturity.value,
                    supported_markets=tuple(item.value for item in capabilities.markets),
                    supported_data_types=tuple(
                        item.data_type.value
                        for item in capabilities.data
                        if item.supports_market(metadata.reference.market_type)
                    ),
                    rate_limit_policy_json=json.dumps(
                        capabilities.rate_limit_policy,
                        sort_keys=True,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
            else:
                return self._unresolved_local_rebuild(
                    parent, manifest, audit_name, evidence, "metadata_type_unsupported"
                )
            replacements.append(
                self._storage.write_partition(
                    dataset_id=parent.dataset_id,
                    version=version,
                    layer="normalized",
                    instrument=manifest.instrument,
                    data_type=partition.data_type,
                    timeframe=None,
                    records=(record,),
                    source_artifact_ids=partition.source_artifact_ids,
                    format_name=format_name,
                    compression=compression,
                )
            )

        damaged_ids = {item.partition_id for item in damaged}
        partitions = tuple(
            sorted(
                (
                    *(item for item in manifest.partitions if item.partition_id not in damaged_ids),
                    *replacements,
                ),
                key=lambda item: (
                    item.data_type.value,
                    item.timeframe or "",
                    item.start,
                    item.end,
                    item.partition_id,
                ),
            )
        )
        rebuilt_manifest = replace(
            manifest,
            version=version,
            partitions=partitions,
            lineage=(
                *manifest.lineage,
                f"parent:{parent.dataset_id}:v{parent.version}:{parent.dataset_hash}",
                "repair:metadata-from-raw",
            ),
            created_at=datetime.now(UTC),
        )
        digest = dataset_hash(rebuilt_manifest)
        manifest_path = self._storage.write_manifest(parent.dataset_id, version, rebuilt_manifest)
        descriptor = DatasetDescriptor(
            dataset_id=parent.dataset_id,
            version=version,
            dataset_hash=digest,
            manifest_uri=manifest_path.relative_to(self._storage.root).as_posix(),
            storage_uri=self._storage.root.as_uri(),
            exchange_id=parent.exchange_id,
            market_type=parent.market_type,
            instrument_id=parent.instrument_id,
            data_types=rebuilt_manifest.data_types,
            timeframes=rebuilt_manifest.timeframes,
            start=rebuilt_manifest.start,
            end=rebuilt_manifest.end,
            schema_version=rebuilt_manifest.schema_version,
            validation_status=rebuilt_manifest.validation.status,
            unresolved_issues=parent.unresolved_issues,
            lineage=rebuilt_manifest.lineage,
            compatible_consumers=parent.compatible_consumers,
        )
        self._registry.register(descriptor)
        audit_path = self._storage.write_repair_audit(
            audit_name,
            {
                "dataset_id": parent.dataset_id,
                "parent_version": parent.version,
                "new_version": version,
                "status": RepairStatus.REPAIRED.value,
                "mode": "metadata_from_raw",
                "raw_artifact_id": raw_descriptor.artifact_id,
                "removed_partition_ids": sorted(damaged_ids),
                "replacement_partition_ids": sorted(item.partition_id for item in replacements),
                "evidence": evidence,
                "new_dataset_hash": digest,
                "created_at": datetime.now(UTC),
            },
        )
        return RepairResult(
            status=RepairStatus.REPAIRED,
            repaired_ranges=tuple(sorted({(item.start, item.end) for item in damaged})),
            unresolved_gaps=tuple(rebuilt_manifest.validation.gaps),
            new_dataset_version=version,
            audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
        )

    def _rebuild_derived_partitions(
        self,
        *,
        parent: DatasetDescriptor,
        manifest: DatasetManifest,
        damaged: tuple[NormalizedPartitionDescriptor, ...],
        evidence: tuple[dict[str, object], ...],
        audit_name: str,
        format_name: str,
        compression: str,
    ) -> RepairResult:
        if any(item.data_type is not DataType.KLINES or item.timeframe is None for item in damaged):
            audit_path = self._storage.write_repair_audit(
                audit_name,
                {
                    "dataset_id": parent.dataset_id,
                    "version": parent.version,
                    "status": "derived_type_unsupported",
                    "evidence": evidence,
                    "created_at": datetime.now(UTC),
                },
            )
            return RepairResult(
                status=RepairStatus.UNRESOLVED,
                repaired_ranges=(),
                unresolved_gaps=tuple(manifest.validation.gaps),
                new_dataset_version=None,
                audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
            )

        normalized_by_id = {
            item.partition_id: item
            for item in manifest.partitions
            if item.storage_uri.startswith("normalized/") and item.data_type is DataType.KLINES
        }
        replacement: list[NormalizedPartitionDescriptor] = []
        repaired_ranges: list[tuple[datetime, datetime]] = []
        version = self._registry.next_version(parent.dataset_id)
        grouped: dict[str, list[NormalizedPartitionDescriptor]] = defaultdict(list)
        for item in damaged:
            if item.timeframe is None:
                return self._unresolved_local_rebuild(
                    parent,
                    manifest,
                    audit_name,
                    evidence,
                    "derived_timeframe_missing",
                )
            grouped[item.timeframe].append(item)

        for target, target_partitions in grouped.items():
            source_ids = tuple(
                dict.fromkeys(
                    source_id
                    for item in target_partitions
                    for source_id in item.source_artifact_ids
                )
            )
            sources = [normalized_by_id.get(source_id) for source_id in source_ids]
            if not source_ids or any(item is None for item in sources):
                return self._unresolved_local_rebuild(
                    parent, manifest, audit_name, evidence, "derived_lineage_missing"
                )
            source_partitions: list[NormalizedPartitionDescriptor] = []
            for source in sources:
                if source is None:
                    return self._unresolved_local_rebuild(
                        parent,
                        manifest,
                        audit_name,
                        evidence,
                        "derived_lineage_missing",
                    )
                issue = self._partition_integrity_issue(self._storage.root.resolve(), source)
                if issue is not None:
                    return self._unresolved_local_rebuild(
                        parent,
                        manifest,
                        audit_name,
                        (*evidence, issue),
                        "derived_source_damaged",
                    )
                source_partitions.append(source)
            for damaged_partition in sorted(
                target_partitions,
                key=lambda item: (item.start, item.end, item.partition_id),
            ):
                rebuilt = self._write_derived_range(
                    dataset_id=parent.dataset_id,
                    version=version,
                    instrument=manifest.instrument,
                    target_timeframe=target,
                    start=damaged_partition.start,
                    end=damaged_partition.end,
                    source_partitions=source_partitions,
                    source_ids=source_ids,
                    format_name=format_name,
                    compression=compression,
                )
                if rebuilt is None:
                    return self._unresolved_local_rebuild(
                        parent,
                        manifest,
                        audit_name,
                        evidence,
                        "derived_rebuild_empty",
                    )
                replacement.append(rebuilt)
                repaired_ranges.append((damaged_partition.start, damaged_partition.end))

        damaged_ids = {item.partition_id for item in damaged}
        partitions = tuple(
            sorted(
                (
                    *(item for item in manifest.partitions if item.partition_id not in damaged_ids),
                    *replacement,
                ),
                key=lambda item: (
                    item.data_type.value,
                    item.timeframe or "",
                    item.start,
                    item.end,
                    item.partition_id,
                ),
            )
        )
        rebuilt_manifest = replace(
            manifest,
            version=version,
            partitions=partitions,
            lineage=(
                *manifest.lineage,
                f"parent:{parent.dataset_id}:v{parent.version}:{parent.dataset_hash}",
                "repair:derived-local-rebuild",
            ),
            created_at=datetime.now(UTC),
        )
        digest = dataset_hash(rebuilt_manifest)
        manifest_path = self._storage.write_manifest(parent.dataset_id, version, rebuilt_manifest)
        descriptor = DatasetDescriptor(
            dataset_id=parent.dataset_id,
            version=version,
            dataset_hash=digest,
            manifest_uri=manifest_path.relative_to(self._storage.root).as_posix(),
            storage_uri=self._storage.root.as_uri(),
            exchange_id=parent.exchange_id,
            market_type=parent.market_type,
            instrument_id=parent.instrument_id,
            data_types=rebuilt_manifest.data_types,
            timeframes=rebuilt_manifest.timeframes,
            start=rebuilt_manifest.start,
            end=rebuilt_manifest.end,
            schema_version=rebuilt_manifest.schema_version,
            validation_status=rebuilt_manifest.validation.status,
            unresolved_issues=parent.unresolved_issues,
            lineage=rebuilt_manifest.lineage,
            compatible_consumers=parent.compatible_consumers,
        )
        self._registry.register(descriptor)
        audit_path = self._storage.write_repair_audit(
            audit_name,
            {
                "dataset_id": parent.dataset_id,
                "parent_version": parent.version,
                "new_version": version,
                "status": RepairStatus.REPAIRED.value,
                "mode": "derived_local_rebuild",
                "removed_partition_ids": sorted(damaged_ids),
                "replacement_partition_ids": sorted(item.partition_id for item in replacement),
                "evidence": evidence,
                "new_dataset_hash": digest,
                "created_at": datetime.now(UTC),
            },
        )
        return RepairResult(
            status=RepairStatus.REPAIRED,
            repaired_ranges=tuple(sorted(set(repaired_ranges))),
            unresolved_gaps=tuple(rebuilt_manifest.validation.gaps),
            new_dataset_version=version,
            audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
        )

    def _unresolved_local_rebuild(
        self,
        parent: DatasetDescriptor,
        manifest: DatasetManifest,
        audit_name: str,
        evidence: tuple[dict[str, object], ...],
        reason: str,
    ) -> RepairResult:
        audit_path = self._storage.write_repair_audit(
            audit_name,
            {
                "dataset_id": parent.dataset_id,
                "version": parent.version,
                "status": reason,
                "evidence": evidence,
                "created_at": datetime.now(UTC),
            },
        )
        return RepairResult(
            status=RepairStatus.UNRESOLVED,
            repaired_ranges=(),
            unresolved_gaps=tuple(manifest.validation.gaps),
            new_dataset_version=None,
            audit_uri=audit_path.relative_to(self._storage.root).as_posix(),
        )

    def _iter_kline_partition(
        self, partition: NormalizedPartitionDescriptor
    ) -> Iterator[KlineRecord]:
        for record in self._storage.iter_partition_records(partition):
            if not isinstance(record, KlineRecord):
                raise StructuredIngestionError(
                    "PARTITION_RECORD_TYPE_MISMATCH",
                    "kline repair source contains a non-kline record",
                    False,
                    "repair",
                    {"partition_id": partition.partition_id},
                )
            yield record

    def _iter_kline_partitions(
        self, partitions: Iterable[NormalizedPartitionDescriptor]
    ) -> Iterator[KlineRecord]:
        previous_end: datetime | None = None
        for partition in sorted(
            partitions,
            key=lambda item: (item.start, item.end, item.partition_id),
        ):
            if previous_end is not None and partition.start < previous_end:
                raise StructuredIngestionError(
                    "PARTITION_RANGE_CONFLICT",
                    "repair source partitions overlap",
                    False,
                    "repair",
                    {"partition_id": partition.partition_id},
                )
            yield from self._iter_kline_partition(partition)
            previous_end = partition.end

    def _write_derived_range(
        self,
        *,
        dataset_id: str,
        version: int,
        instrument: InstrumentReference,
        target_timeframe: str,
        start: datetime,
        end: datetime,
        source_partitions: Iterable[NormalizedPartitionDescriptor],
        source_ids: tuple[str, ...],
        format_name: str,
        compression: str,
    ) -> NormalizedPartitionDescriptor | None:
        aggregated = aggregate_klines_stream(
            self._iter_kline_partitions(source_partitions),
            target_timeframe=target_timeframe,
        )
        selected = (item for item in aggregated if start <= item.open_time < end)
        try:
            first = next(selected)
        except StopIteration:
            return None
        return self._storage.write_partition(
            dataset_id=dataset_id,
            version=version,
            layer="derived",
            instrument=instrument,
            data_type=DataType.KLINES,
            timeframe=target_timeframe,
            records=chain((first,), selected),
            source_artifact_ids=source_ids,
            format_name=format_name,
            compression=compression,
        )

    def _descriptor(self, dataset_id: str, version: int | None) -> DatasetDescriptor:
        payload = self._registry.inspect(dataset_id, version)
        if payload is None:
            raise RegistryError(
                "DATASET_NOT_FOUND",
                "registered dataset was not found",
                False,
                "inspect",
            )
        return descriptor_from_dict(payload)

    @staticmethod
    def _base_timeframe(partitions: Iterable[object]) -> str:
        from .contracts import NormalizedPartitionDescriptor

        candidates = [
            item.timeframe
            for item in partitions
            if isinstance(item, NormalizedPartitionDescriptor)
            and item.timeframe is not None
            and item.storage_uri.startswith("normalized/")
        ]
        if not candidates:
            raise StructuredIngestionError(
                "BASE_TIMEFRAME_UNAVAILABLE",
                "registered dataset has no normalized interval timeframe",
                False,
                "planning",
            )
        return min(candidates, key=lambda item: (timeframe_seconds(item), item))

    @staticmethod
    def _utc(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(UTC)

    @staticmethod
    def _request_id(
        operation: str,
        parent: DatasetDescriptor,
        end: datetime,
        data_types: Iterable[DataType],
    ) -> str:
        types = "-".join(sorted(item.value for item in data_types))
        return (
            f"{operation}:{parent.dataset_id}:v{parent.version}:"
            f"{types}:{end.strftime('%Y%m%dT%H%M%SZ')}"
        )
