from __future__ import annotations

import logging
from collections import defaultdict
from collections.abc import Iterable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from itertools import groupby
from pathlib import Path
from typing import cast

from .adapters.base import AcquiredData, ExchangeAdapter
from .aggregation import aggregate_klines_stream
from .canonical import sha256_canonical
from .checkpoint import load_checkpoint
from .contracts import (
    DatasetDescriptor,
    DatasetManifest,
    DataTypeValidationReport,
    DataValidationReport,
    DownloadCheckpoint,
    DownloadChunk,
    ExchangeReference,
    IngestionResumeSpec,
    IngestionRunResult,
    InstrumentReference,
    MarketDataRequest,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
    ValidationIssue,
)
from .enums import DataType, RunState, Severity, ValidationStatus
from .errors import AdapterError, StructuredIngestionError
from .hashing import dataset_hash
from .manifest_io import load_manifest
from .observability import configure_logging
from .publication_admission import (
    PartitionPublicationPlan,
    assert_publication_matches_plan,
    plan_monthly_partitions,
    require_publication_capacity,
)
from .records import (
    ExchangeMetadataRecord,
    InstrumentMetadataRecord,
    KlineRecord,
    NormalizedRecord,
)
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .serialization import json_bytes
from .spool import RecordSpool
from .state_machine import IngestionStateMachine
from .storage import LocalStorage
from .validation import (
    apply_gap_policy,
    combine_validation_reports,
    validate_sorted_records,
)
from .version import (
    MANIFEST_SCHEMA_VERSION,
    NORMALIZATION_VERSION,
    VALIDATION_POLICY_VERSION,
)

_POINT_EVENT_TYPES = frozenset(
    {
        DataType.FUNDING_RATE,
        DataType.INSTRUMENT_METADATA,
        DataType.EXCHANGE_METADATA,
    }
)
_INTERVAL_TYPES = frozenset(
    {
        DataType.KLINES,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
)
_SERIES_TYPES = _POINT_EVENT_TYPES | _INTERVAL_TYPES
_METADATA_TYPES = frozenset({DataType.INSTRUMENT_METADATA, DataType.EXCHANGE_METADATA})


class IngestionService:
    """Deterministic historical ingestion orchestration for registered datasets."""

    def __init__(
        self,
        *,
        adapters: Mapping[str, ExchangeAdapter],
        storage: LocalStorage,
        registry: SQLiteDatasetRegistry,
        logger: logging.Logger | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._storage = storage
        self._registry = registry
        self._logger = logger or configure_logging()

    def adapter(self, exchange_id: str) -> ExchangeAdapter:
        adapter = self._adapters.get(exchange_id)
        if adapter is None:
            raise AdapterError(
                "EXCHANGE_UNSUPPORTED",
                f"no adapter is registered for {exchange_id}",
                False,
                "capability_discovery",
            )
        return adapter

    def execute(
        self,
        request: MarketDataRequest,
        *,
        format_name: str = "parquet",
        compression: str = "zstd",
        reject_unresolved_required_gaps: bool = True,
        concurrency: int = 1,
        max_records_in_memory: int = 5_000_000,
        base_dataset: DatasetDescriptor | None = None,
        base_manifest_override: DatasetManifest | None = None,
    ) -> IngestionRunResult:
        if not isinstance(concurrency, int) or isinstance(concurrency, bool) or concurrency <= 0:
            raise ValueError("concurrency must be a positive integer")
        if (
            not isinstance(max_records_in_memory, int)
            or isinstance(max_records_in_memory, bool)
            or max_records_in_memory <= 0
        ):
            raise ValueError("max_records_in_memory must be a positive integer")
        machine = IngestionStateMachine()
        adapter = self._adapters.get(request.exchange_id)
        if adapter is None:
            raise AdapterError(
                "EXCHANGE_UNSUPPORTED",
                f"no adapter is registered for {request.exchange_id}",
                False,
                "capability_discovery",
            )
        execution_hash = sha256_canonical(
            {
                "request": request,
                "adapter_id": adapter.exchange_id,
                "adapter_version": adapter.adapter_version,
                "normalization_version": NORMALIZATION_VERSION,
                "validation_policy_version": VALIDATION_POLICY_VERSION,
                "storage_format": format_name,
                "compression": compression,
                "reject_unresolved_required_gaps": reject_unresolved_required_gaps,
                "concurrency": concurrency,
                "max_records_in_memory": max_records_in_memory,
            }
        )
        run_id = f"mdi:{execution_hash[:24]}"
        completed_chunks: list[str] = []
        pending_chunks: list[str] = []
        warnings: list[str] = []
        checkpoint_path = self._storage.root / "checkpoints" / f"{run_id}.json"
        base_manifest: DatasetManifest | None = None
        if base_manifest_override is not None and base_dataset is None:
            raise ValueError("base_manifest_override requires base_dataset")

        with self._storage.lock(request.request_id):
            machine.transition(RunState.VALIDATING_REQUEST)
            existing = self._registry.claim_request(request.request_id, execution_hash)
            if existing is not None:
                descriptor = descriptor_from_dict(existing)
                return IngestionRunResult(
                    run_id=run_id,
                    final_state=RunState.COMPLETED_WITH_WARNINGS,
                    dataset=descriptor,
                    validation=None,
                    warnings=("identical request reused an already registered dataset",),
                    completed_at=datetime.now(UTC),
                )
            if checkpoint_path.is_file():
                checkpoint = load_checkpoint(checkpoint_path)
                if checkpoint.request_hash != execution_hash:
                    raise StructuredIngestionError(
                        "CHECKPOINT_IDENTITY_MISMATCH",
                        "checkpoint request hash does not match the current request",
                        False,
                        "checkpoint",
                    )
                completed_chunks = list(checkpoint.completed_chunks)
                warnings.append(f"resuming from checkpoint state {checkpoint.state.value}")

            machine.transition(RunState.DISCOVERING_CAPABILITIES)
            capabilities = adapter.get_capabilities()
            if request.market_type not in capabilities.markets:
                raise AdapterError(
                    "MARKET_UNSUPPORTED",
                    "adapter does not advertise the requested market",
                    False,
                    "capability_discovery",
                )
            advertised = {
                item.data_type
                for item in capabilities.data
                if item.supports_market(request.market_type)
            }
            missing_required = sorted(
                item.value for item in request.required_data if item not in advertised
            )
            if missing_required:
                raise AdapterError(
                    "REQUIRED_DATA_UNSUPPORTED",
                    "adapter cannot provide all required data types for the requested market",
                    False,
                    "capability_discovery",
                    {"missing": missing_required},
                )
            supported_optional = tuple(item for item in request.optional_data if item in advertised)
            missing_optional = tuple(
                item for item in request.optional_data if item not in advertised
            )
            if missing_optional:
                warnings.append(
                    "optional data unavailable: "
                    + ", ".join(item.value for item in missing_optional)
                )

            machine.transition(RunState.RESOLVING_INSTRUMENT)
            resolution = adapter.resolve_instrument_with_evidence(
                request.market_type, request.symbol
            )
            metadata = resolution.metadata
            resolved_request = replace(request, optional_data=supported_optional)
            if request.full_available_history and request.start is None:
                if metadata.listing_date is None:
                    raise AdapterError(
                        "LISTING_DATE_UNAVAILABLE",
                        "full history requires a verified instrument listing date",
                        False,
                        "instrument_resolution",
                    )
                resolved_request = replace(resolved_request, start=metadata.listing_date)
            if metadata.listing_date is not None and resolved_request.end <= metadata.listing_date:
                raise AdapterError(
                    "RANGE_BEFORE_LISTING",
                    "requested range ends before the verified listing date",
                    False,
                    "instrument_resolution",
                    {"listing_date": metadata.listing_date.isoformat()},
                )
            if (
                metadata.listing_date is not None
                and resolved_request.start is not None
                and resolved_request.start < metadata.listing_date
            ):
                resolved_request = replace(resolved_request, start=metadata.listing_date)
                warnings.append("requested start was clipped to verified listing date")
            if (
                metadata.delisting_date is not None
                and resolved_request.start is not None
                and resolved_request.start >= metadata.delisting_date
            ):
                raise AdapterError(
                    "RANGE_AFTER_DELISTING",
                    "requested range starts after the verified delisting date",
                    False,
                    "instrument_resolution",
                    {"delisting_date": metadata.delisting_date.isoformat()},
                )
            if (
                metadata.delisting_date is not None
                and resolved_request.end > metadata.delisting_date
            ):
                resolved_request = replace(resolved_request, end=metadata.delisting_date)
                warnings.append("requested end was clipped to verified delisting date")

            if base_dataset is not None:
                if (
                    base_dataset.exchange_id != metadata.reference.exchange_id
                    or base_dataset.market_type is not metadata.reference.market_type
                    or base_dataset.instrument_id != metadata.reference.canonical_id
                ):
                    raise StructuredIngestionError(
                        "BASE_DATASET_IDENTITY_MISMATCH",
                        "base dataset does not match the resolved exchange instrument",
                        False,
                        "planning",
                    )
                base_manifest = (
                    base_manifest_override
                    if base_manifest_override is not None
                    else load_manifest(self._storage.root, base_dataset)
                )
                if (
                    base_manifest.dataset_id != base_dataset.dataset_id
                    or base_manifest.version != base_dataset.version
                ):
                    raise StructuredIngestionError(
                        "BASE_MANIFEST_IDENTITY_MISMATCH",
                        "base manifest override differs from the registered descriptor",
                        False,
                        "planning",
                    )
                if base_manifest.normalization_version != NORMALIZATION_VERSION:
                    raise StructuredIngestionError(
                        "NORMALIZATION_VERSION_MISMATCH",
                        "base dataset normalization version is incompatible with this update",
                        False,
                        "planning",
                    )

            machine.transition(RunState.PLANNING)
            plan = adapter.plan_download(resolved_request, metadata.reference)
            warnings.extend(plan.limitations)
            resume_spec_path = self._storage.root / "runs" / f"{run_id}.json"
            if not resume_spec_path.is_file():
                self._storage.write_resume_spec(
                    run_id,
                    IngestionResumeSpec(
                        run_id=run_id,
                        execution_hash=execution_hash,
                        request=request,
                        adapter_version=adapter.adapter_version,
                        storage_format=format_name,
                        compression=compression,
                        reject_unresolved_required_gaps=(reject_unresolved_required_gaps),
                        created_at=datetime.now(UTC),
                        concurrency=concurrency,
                        max_records_in_memory=max_records_in_memory,
                        base_dataset=base_dataset,
                        base_manifest_override=base_manifest_override,
                        resume_spec_version=3,
                    ),
                )
            known_chunk_ids = {chunk.chunk_id for chunk in plan.chunks}
            completed_chunks = [item for item in completed_chunks if item in known_chunk_ids]
            pending_chunks = [
                chunk.chunk_id for chunk in plan.chunks if chunk.chunk_id not in completed_chunks
            ]
            machine.transition(RunState.DOWNLOADING)
            self._write_checkpoint(
                run_id,
                execution_hash,
                machine.state,
                completed_chunks,
                pending_chunks,
            )
            cancelled = self._cancelled_result(
                machine=machine,
                run_id=run_id,
                execution_hash=execution_hash,
                completed_chunks=completed_chunks,
                pending_chunks=pending_chunks,
                warnings=warnings,
            )
            if cancelled is not None:
                return cancelled

            with RecordSpool(
                self._storage.root / "staging" / f"{execution_hash}.sqlite3",
                reset=True,
            ) as spool:
                raw_artifacts: list[RawArtifactDescriptor] = []
                source_ids_by_type: dict[DataType, list[str]] = defaultdict(list)
                metadata_effective_at = resolved_request.end
                metadata_chunks = tuple(
                    DownloadChunk(
                        chunk_id=f"{request.request_id}:{data_type.value}:snapshot",
                        data_type=data_type,
                        start=metadata_effective_at - timedelta(microseconds=1),
                        end=metadata_effective_at,
                        source=resolution.source,
                        expected_uri=resolution.source_uri,
                    )
                    for data_type in (
                        DataType.INSTRUMENT_METADATA,
                        DataType.EXCHANGE_METADATA,
                    )
                )
                for metadata_chunk in metadata_chunks:
                    restored_metadata = self._storage.read_raw(metadata.reference, metadata_chunk)
                    if restored_metadata is None:
                        metadata_artifact = self._storage.write_raw(
                            run_id=run_id,
                            instrument=metadata.reference,
                            chunk=metadata_chunk,
                            payload=resolution.raw_payload,
                            source_uri=resolution.source_uri,
                            adapter_version=adapter.adapter_version,
                            response_metadata=resolution.response_metadata,
                            upstream_checksum=resolution.upstream_checksum,
                            source_kind=resolution.source,
                        )
                    else:
                        _, metadata_artifact = restored_metadata
                    raw_artifacts.append(metadata_artifact)
                    source_ids_by_type[metadata_chunk.data_type].append(
                        metadata_artifact.artifact_id
                    )

                def load_chunk(
                    chunk: DownloadChunk,
                ) -> tuple[AcquiredData, RawArtifactDescriptor | None, bool]:
                    restored = (
                        self._storage.read_raw(metadata.reference, chunk)
                        if chunk.chunk_id in completed_chunks
                        else None
                    )
                    if restored is None:
                        acquired = adapter.acquire(chunk, metadata.reference)
                        self._validate_acquired_chunk(chunk, acquired.chunk)
                        return acquired, None, False
                    raw_payload, raw_artifact = restored
                    restore_chunk = replace(
                        chunk,
                        source=raw_artifact.source,
                        expected_uri=raw_artifact.source_uri,
                    )
                    acquired = adapter.restore(
                        restore_chunk,
                        metadata.reference,
                        raw_payload,
                    )
                    self._validate_acquired_chunk(restore_chunk, acquired.chunk)
                    return acquired, raw_artifact, True

                try:
                    planned_chunks = tuple(plan.chunks)
                    for offset in range(0, len(planned_chunks), concurrency):
                        cancelled = self._cancelled_result(
                            machine=machine,
                            run_id=run_id,
                            execution_hash=execution_hash,
                            completed_chunks=completed_chunks,
                            pending_chunks=pending_chunks,
                            warnings=warnings,
                        )
                        if cancelled is not None:
                            return cancelled
                        batch = planned_chunks[offset : offset + concurrency]
                        outcomes: tuple[
                            tuple[
                                DownloadChunk,
                                tuple[
                                    AcquiredData,
                                    RawArtifactDescriptor | None,
                                    bool,
                                ]
                                | None,
                                Exception | None,
                            ],
                            ...,
                        ]
                        if len(batch) == 1:
                            outcomes = ((batch[0], load_chunk(batch[0]), None),)
                        else:
                            with ThreadPoolExecutor(
                                max_workers=len(batch),
                                thread_name_prefix="mdi-download",
                            ) as executor:
                                futures = tuple(
                                    (chunk, executor.submit(load_chunk, chunk)) for chunk in batch
                                )
                                collected: list[
                                    tuple[
                                        DownloadChunk,
                                        tuple[
                                            AcquiredData,
                                            RawArtifactDescriptor | None,
                                            bool,
                                        ]
                                        | None,
                                        Exception | None,
                                    ]
                                ] = []
                                for chunk, future in futures:
                                    try:
                                        collected.append((chunk, future.result(), None))
                                    except Exception as exc:
                                        collected.append((chunk, None, exc))
                                outcomes = tuple(collected)

                        first_error: Exception | None = None
                        for chunk, outcome, error in outcomes:
                            if error is not None:
                                if first_error is None:
                                    first_error = error
                                continue
                            if not isinstance(outcome, tuple) or len(outcome) != 3:
                                raise StructuredIngestionError(
                                    "DOWNLOAD_OUTCOME_INVALID",
                                    "chunk worker returned an invalid outcome",
                                    False,
                                    "download",
                                )
                            acquired, raw_artifact, was_restored = outcome
                            if raw_artifact is None:
                                raw_artifact = self._storage.write_raw(
                                    run_id=run_id,
                                    instrument=metadata.reference,
                                    chunk=chunk,
                                    payload=acquired.raw_payload,
                                    source_uri=acquired.source_uri,
                                    adapter_version=adapter.adapter_version,
                                    response_metadata=acquired.response_metadata,
                                    upstream_checksum=acquired.upstream_checksum,
                                    source_kind=acquired.chunk.source,
                                )
                                if chunk.chunk_id not in completed_chunks:
                                    completed_chunks.append(chunk.chunk_id)
                                if chunk.chunk_id in pending_chunks:
                                    pending_chunks.remove(chunk.chunk_id)
                                self._write_checkpoint(
                                    run_id,
                                    execution_hash,
                                    machine.state,
                                    completed_chunks,
                                    pending_chunks,
                                )
                            self._logger.info(
                                (
                                    "chunk restored from immutable raw"
                                    if was_restored
                                    else "chunk downloaded"
                                ),
                                extra={
                                    "context": {
                                        "run_id": run_id,
                                        "request_id": request.request_id,
                                        "exchange_id": request.exchange_id,
                                        "instrument_id": metadata.reference.canonical_id,
                                        "chunk_id": chunk.chunk_id,
                                        "data_type": chunk.data_type.value,
                                        (
                                            "rows_restored" if was_restored else "rows_downloaded"
                                        ): len(acquired.records),
                                        "downloaded_bytes": (
                                            0 if was_restored else len(acquired.raw_payload)
                                        ),
                                    }
                                },
                            )
                            raw_artifacts.append(raw_artifact)
                            source_ids_by_type[chunk.data_type].append(raw_artifact.artifact_id)
                            if len(acquired.records) > max_records_in_memory:
                                raise AdapterError(
                                    "IN_MEMORY_RECORD_LIMIT",
                                    "one adapter chunk exceeded the configured memory bound",
                                    False,
                                    "normalize",
                                    {
                                        "maximum_records": max_records_in_memory,
                                        "observed_records": len(acquired.records),
                                        "chunk_id": chunk.chunk_id,
                                    },
                                )
                            spool.append("acquired", acquired.records)
                        if first_error is not None:
                            if isinstance(first_error, StructuredIngestionError):
                                raise first_error
                            raise StructuredIngestionError(
                                "DOWNLOAD_WORKER_FAILED",
                                "concurrent chunk worker failed unexpectedly",
                                False,
                                "download",
                                {"error_type": type(first_error).__name__},
                            ) from first_error

                    verified_metadata = adapter.resolve_instrument(
                        request.market_type, request.symbol
                    )
                    if sha256_canonical(verified_metadata) != sha256_canonical(metadata):
                        raise AdapterError(
                            "INSTRUMENT_METADATA_CHANGED",
                            "instrument metadata changed during historical acquisition",
                            True,
                            "verify_raw",
                            {
                                "before": sha256_canonical(metadata),
                                "after": sha256_canonical(verified_metadata),
                            },
                        )
                except StructuredIngestionError:
                    machine.transition(RunState.PAUSED)
                    self._write_checkpoint(
                        run_id,
                        execution_hash,
                        machine.state,
                        completed_chunks,
                        pending_chunks,
                    )
                    raise
                except Exception as exc:
                    machine.transition(RunState.PAUSED)
                    self._write_checkpoint(
                        run_id,
                        execution_hash,
                        machine.state,
                        completed_chunks,
                        pending_chunks,
                    )
                    raise StructuredIngestionError(
                        "UNEXPECTED_ACQUISITION_FAILURE",
                        "historical acquisition failed with an unexpected typed boundary error",
                        False,
                        "download",
                        {"error_type": type(exc).__name__},
                    ) from exc

                spool.append(
                    "acquired",
                    (
                        InstrumentMetadataRecord(
                            effective_at=metadata_effective_at,
                            symbol=metadata.reference.symbol,
                            exchange_id=metadata.reference.exchange_id,
                            market_type=metadata.reference.market_type.value,
                            source_kind=resolution.source.value,
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
                        ),
                    ),
                )
                spool.append(
                    "acquired",
                    (
                        ExchangeMetadataRecord(
                            effective_at=metadata_effective_at,
                            symbol=metadata.reference.symbol,
                            exchange_id=metadata.reference.exchange_id,
                            market_type=metadata.reference.market_type.value,
                            source_kind=resolution.source.value,
                            adapter_version=adapter.adapter_version,
                            maturity=capabilities.maturity.value,
                            supported_markets=tuple(item.value for item in capabilities.markets),
                            supported_data_types=tuple(
                                item.data_type.value
                                for item in capabilities.data
                                if item.supports_market(metadata.reference.market_type)
                            ),
                            rate_limit_policy_json=__import__("json").dumps(
                                capabilities.rate_limit_policy,
                                sort_keys=True,
                                separators=(",", ":"),
                                allow_nan=False,
                            ),
                        ),
                    ),
                )

                machine.transition(RunState.VERIFYING_RAW)
                requested_series = tuple(
                    item
                    for item in (
                        *resolved_request.required_data,
                        *resolved_request.optional_data,
                    )
                    if item in _SERIES_TYPES
                )
                requested_series = tuple(
                    dict.fromkeys(
                        (
                            *requested_series,
                            DataType.INSTRUMENT_METADATA,
                            DataType.EXCHANGE_METADATA,
                        )
                    )
                )
                if not requested_series:
                    raise AdapterError(
                        "NO_SERIES_REQUESTED",
                        "request contains no implemented historical data stream",
                        False,
                        "verify_raw",
                    )

                machine.transition(RunState.NORMALIZING)
                machine.transition(RunState.VALIDATING_DATA)
                accepted_data_types: set[DataType] = set()
                detailed_reports: list[DataTypeValidationReport] = []
                required_reports: list[DataValidationReport] = []
                accepted_optional_reports: list[DataValidationReport] = []
                aggregate_extra_issues: list[ValidationIssue] = []
                rejected_required = False
                planned_bounds: dict[
                    tuple[DataType, str | None],
                    tuple[datetime, datetime],
                ] = {}
                for planned_chunk in plan.chunks:
                    if planned_chunk.data_type in _METADATA_TYPES:
                        continue
                    key = (planned_chunk.data_type, planned_chunk.timeframe)
                    current_bounds = planned_bounds.get(key)
                    if current_bounds is None:
                        planned_bounds[key] = (
                            planned_chunk.start,
                            planned_chunk.end,
                        )
                    else:
                        planned_bounds[key] = (
                            min(current_bounds[0], planned_chunk.start),
                            max(current_bounds[1], planned_chunk.end),
                        )

                required_set = set(resolved_request.required_data) | _METADATA_TYPES
                for data_type in requested_series:
                    timeframe = self._timeframe_for(data_type, resolved_request.base_timeframe)
                    record_count = spool.count("acquired", data_type, timeframe)
                    expected_bounds = planned_bounds.get((data_type, timeframe))
                    if (
                        record_count == 0
                        and data_type in _INTERVAL_TYPES
                        and expected_bounds is not None
                    ):
                        report = validate_sorted_records(
                            (),
                            data_type=data_type,
                            instrument=metadata.reference,
                            timeframe=timeframe,
                            allow_provisional=resolved_request.allow_provisional,
                            expected_start=expected_bounds[0],
                            expected_end=expected_bounds[1],
                            listing_date=metadata.listing_date,
                            delisting_date=metadata.delisting_date,
                        )
                        if data_type in required_set:
                            report = apply_gap_policy(
                                report,
                                reject_unresolved=reject_unresolved_required_gaps,
                            )
                    elif record_count == 0:
                        is_required = data_type in required_set
                        severity = Severity.ERROR if is_required else Severity.WARNING
                        report = DataValidationReport(
                            status=(
                                ValidationStatus.INVALID
                                if is_required
                                else ValidationStatus.VALID_WITH_WARNINGS
                            ),
                            issues=(
                                ValidationIssue(
                                    code=(
                                        "required_data_empty"
                                        if is_required
                                        else "optional_data_empty"
                                    ),
                                    severity=severity,
                                    message=(
                                        f"source returned no {data_type.value} records "
                                        "for the effective requested range"
                                    ),
                                ),
                            ),
                            gaps=(),
                            duplicates=(),
                            rows_checked=0,
                            created_at=datetime.now(UTC),
                        )
                    else:
                        accepted_batch: list[NormalizedRecord] = []

                        def accept_record(
                            record: NormalizedRecord,
                            batch: list[NormalizedRecord] = accepted_batch,
                        ) -> None:
                            batch.append(record)
                            if len(batch) >= 1_000:
                                spool.append("validated", batch)
                                batch.clear()

                        report = validate_sorted_records(
                            spool.iter_sorted("acquired", data_type, timeframe),
                            data_type=data_type,
                            instrument=metadata.reference,
                            timeframe=timeframe,
                            accept=accept_record,
                            allow_provisional=resolved_request.allow_provisional,
                            expected_start=(
                                expected_bounds[0] if expected_bounds is not None else None
                            ),
                            expected_end=(
                                expected_bounds[1] if expected_bounds is not None else None
                            ),
                            listing_date=metadata.listing_date,
                            delisting_date=metadata.delisting_date,
                        )
                        if accepted_batch:
                            spool.append("validated", accepted_batch)
                        if data_type in required_set:
                            report = apply_gap_policy(
                                report,
                                reject_unresolved=reject_unresolved_required_gaps,
                            )

                    detailed_reports.append(
                        DataTypeValidationReport(
                            data_type=data_type,
                            timeframe=timeframe,
                            report=report,
                        )
                    )
                    if data_type in required_set:
                        required_reports.append(report)
                        if report.status in {
                            ValidationStatus.INVALID,
                            ValidationStatus.REJECTED,
                        }:
                            rejected_required = True
                        if reject_unresolved_required_gaps and report.gaps:
                            rejected_required = True
                    elif report.status in {
                        ValidationStatus.INVALID,
                        ValidationStatus.REJECTED,
                    }:
                        warnings.append(
                            f"optional {data_type.value} stream was excluded after "
                            f"validation status {report.status.value}"
                        )
                        aggregate_extra_issues.append(
                            ValidationIssue(
                                code="optional_stream_excluded",
                                severity=Severity.WARNING,
                                message=(
                                    f"optional {data_type.value} stream was not "
                                    "published because it failed validation"
                                ),
                            )
                        )
                        spool.clear_stream("validated", data_type, timeframe)
                    else:
                        accepted_optional_reports.append(report)
                        if report.status is ValidationStatus.VALID_WITH_WARNINGS:
                            warnings.append(
                                f"optional {data_type.value} validation status: "
                                f"{report.status.value}"
                            )

                    if spool.count("validated", data_type, timeframe) > 0:
                        accepted_data_types.add(data_type)

                aggregate_inputs = [
                    *required_reports,
                    *accepted_optional_reports,
                ]
                if aggregate_extra_issues:
                    aggregate_inputs.append(
                        DataValidationReport(
                            status=ValidationStatus.VALID_WITH_WARNINGS,
                            issues=tuple(aggregate_extra_issues),
                            gaps=(),
                            duplicates=(),
                            rows_checked=0,
                            created_at=datetime.now(UTC),
                        )
                    )
                validation = combine_validation_reports(aggregate_inputs)

                if rejected_required:
                    machine.transition(RunState.DATASET_REJECTED)
                    self._write_checkpoint(
                        run_id,
                        execution_hash,
                        machine.state,
                        completed_chunks,
                        pending_chunks,
                    )
                    return IngestionRunResult(
                        run_id=run_id,
                        final_state=machine.state,
                        dataset=None,
                        validation=validation,
                        warnings=tuple(warnings),
                        completed_at=datetime.now(UTC),
                    )
                if validation.status in {
                    ValidationStatus.INVALID,
                    ValidationStatus.REJECTED,
                }:
                    raise StructuredIngestionError(
                        "AGGREGATE_VALIDATION_CONTRACT",
                        "accepted streams produced an invalid aggregate report",
                        False,
                        "validation",
                    )
                if validation.status is ValidationStatus.VALID_WITH_WARNINGS:
                    warnings.append(f"aggregate validation status: {validation.status.value}")

                dataset_id = (
                    base_dataset.dataset_id
                    if base_dataset is not None
                    else self._dataset_id(resolved_request)
                )
                version = self._registry.next_version(dataset_id)
                all_partitions: list[NormalizedPartitionDescriptor] = [
                    item
                    for item in (base_manifest.partitions if base_manifest is not None else ())
                    if item.data_type not in _METADATA_TYPES
                ]
                base_kline_partitions: tuple[NormalizedPartitionDescriptor, ...] = ()

                planned_by_stream: dict[
                    tuple[str, DataType, str | None],
                    tuple[PartitionPublicationPlan, ...],
                ] = {}
                publication_plans: list[PartitionPublicationPlan] = []
                planned_base_kline_partitions: tuple[NormalizedPartitionDescriptor, ...] = ()
                for data_type in requested_series:
                    if data_type not in accepted_data_types:
                        continue
                    timeframe = self._timeframe_for(data_type, resolved_request.base_timeframe)
                    stream_plans = plan_monthly_partitions(
                        storage=self._storage,
                        dataset_id=dataset_id,
                        version=version,
                        layer="normalized",
                        instrument=metadata.reference,
                        data_type=data_type,
                        timeframe=timeframe,
                        records=spool.iter_sorted("validated", data_type, timeframe),
                        source_artifact_ids=tuple(
                            dict.fromkeys(source_ids_by_type.get(data_type, ()))
                        ),
                        format_name=format_name,
                        compression=compression,
                    )
                    planned_by_stream[("normalized", data_type, timeframe)] = stream_plans
                    publication_plans.extend(stream_plans)
                    if data_type is DataType.KLINES:
                        planned_base_kline_partitions = (
                            *planned_base_kline_partitions,
                            *(item.descriptor for item in stream_plans),
                        )

                if resolved_request.derived_timeframes and DataType.KLINES in accepted_data_types:
                    for target in resolved_request.derived_timeframes:
                        base_stream = (
                            cast(KlineRecord, item)
                            for item in spool.iter_sorted(
                                "validated",
                                DataType.KLINES,
                                resolved_request.base_timeframe,
                            )
                        )
                        stream_plans = plan_monthly_partitions(
                            storage=self._storage,
                            dataset_id=dataset_id,
                            version=version,
                            layer="derived",
                            instrument=metadata.reference,
                            data_type=DataType.KLINES,
                            timeframe=target,
                            records=aggregate_klines_stream(
                                base_stream,
                                target_timeframe=target,
                            ),
                            source_artifact_ids=tuple(
                                item.partition_id for item in planned_base_kline_partitions
                            ),
                            format_name=format_name,
                            compression=compression,
                        )
                        planned_by_stream[("derived", DataType.KLINES, target)] = stream_plans
                        publication_plans.extend(stream_plans)

                planned_all_partitions = [
                    *all_partitions,
                    *(item.descriptor for item in publication_plans),
                ]
                if not planned_all_partitions:
                    raise StructuredIngestionError(
                        "NO_VALIDATED_PARTITIONS",
                        "no validated historical stream was available for publication",
                        False,
                        "hashing",
                    )

                parent_reports = tuple(
                    item
                    for item in (
                        base_manifest.data_type_reports if base_manifest is not None else ()
                    )
                    if item.data_type not in _METADATA_TYPES
                )
                merged_reports = self._merge_data_type_reports((*parent_reports, *detailed_reports))
                full_validation = combine_validation_reports(
                    (
                        *((base_manifest.validation,) if base_manifest is not None else ()),
                        validation,
                    )
                )
                combined_artifacts = tuple(
                    {
                        item.artifact_id: item
                        for item in (
                            *(base_manifest.source_artifacts if base_manifest is not None else ()),
                            *raw_artifacts,
                        )
                    }.values()
                )

                self._assert_partition_non_overlap(planned_all_partitions)
                planned_ordered_partitions = tuple(
                    sorted(
                        planned_all_partitions,
                        key=lambda item: (
                            item.data_type.value,
                            item.timeframe or "",
                            item.start,
                            item.end,
                            item.partition_id,
                        ),
                    )
                )
                published_data_types = tuple(
                    sorted(
                        {item.data_type for item in planned_ordered_partitions},
                        key=lambda item: item.value,
                    )
                )
                published_timeframes = tuple(
                    sorted(
                        {
                            item.timeframe
                            for item in planned_ordered_partitions
                            if item.timeframe is not None
                        },
                        key=self._timeframe_sort_key,
                    )
                )
                coverage_partitions = (
                    tuple(
                        item
                        for item in planned_ordered_partitions
                        if item.data_type not in _METADATA_TYPES
                    )
                    or planned_ordered_partitions
                )
                coverage_start = min(item.start for item in coverage_partitions)
                coverage_end = max(item.end for item in coverage_partitions)
                unresolved_codes = tuple(
                    dict.fromkeys(
                        issue.code
                        for item in merged_reports
                        for issue in item.report.issues
                        if issue.severity in {Severity.ERROR, Severity.CRITICAL}
                    )
                )
                manifest_created_at = datetime.now(UTC)
                manifest = DatasetManifest(
                    dataset_id=dataset_id,
                    version=version,
                    exchange=ExchangeReference(adapter.exchange_id, adapter.adapter_version),
                    market_type=metadata.reference.market_type,
                    instrument=metadata.reference,
                    data_types=published_data_types,
                    timeframes=published_timeframes,
                    start=coverage_start,
                    end=coverage_end,
                    partitions=planned_ordered_partitions,
                    source_artifacts=combined_artifacts,
                    validation=full_validation,
                    normalization_version=NORMALIZATION_VERSION,
                    validation_policy_version=VALIDATION_POLICY_VERSION,
                    schema_version=MANIFEST_SCHEMA_VERSION,
                    lineage=(
                        f"request:{request.canonical_hash}",
                        f"execution:{execution_hash}",
                        f"effective-request:{resolved_request.canonical_hash}",
                        f"adapter:{adapter.exchange_id}:{adapter.adapter_version}",
                        f"validation-policy:{VALIDATION_POLICY_VERSION}",
                        *(
                            (
                                "parent:"
                                f"{base_dataset.dataset_id}:"
                                f"v{base_dataset.version}:"
                                f"{base_dataset.dataset_hash}",
                            )
                            if base_dataset is not None
                            else ()
                        ),
                        *tuple(f"raw:{item.checksum_sha256}" for item in raw_artifacts),
                    ),
                    created_at=manifest_created_at,
                    data_type_reports=merged_reports,
                )
                manifest_byte_size = len(json_bytes(manifest, pretty=True))
                require_publication_capacity(
                    storage=self._storage,
                    registry_path=self._registry.path,
                    plans=publication_plans,
                    manifest_byte_size=manifest_byte_size,
                    format_name=format_name,
                )

                for data_type in requested_series:
                    if data_type not in accepted_data_types:
                        continue
                    timeframe = self._timeframe_for(data_type, resolved_request.base_timeframe)
                    plans = planned_by_stream[("normalized", data_type, timeframe)]
                    partitions = self._write_monthly(
                        dataset_id=dataset_id,
                        version=version,
                        layer="normalized",
                        instrument=metadata.reference,
                        data_type=data_type,
                        timeframe=timeframe,
                        records=spool.iter_sorted("validated", data_type, timeframe),
                        source_ids=tuple(dict.fromkeys(source_ids_by_type.get(data_type, ()))),
                        format_name=format_name,
                        compression=compression,
                    )
                    self._assert_publication_plan(plans, partitions)
                    all_partitions.extend(partitions)
                    if data_type is DataType.KLINES:
                        base_kline_partitions = (*base_kline_partitions, *partitions)

                if resolved_request.derived_timeframes:
                    machine.transition(RunState.BUILDING_DERIVED)
                    if DataType.KLINES not in accepted_data_types:
                        warnings.append(
                            "derived timeframes were requested but no validated traded "
                            "kline stream is available"
                        )
                    else:
                        for target in resolved_request.derived_timeframes:
                            base_stream = (
                                cast(KlineRecord, item)
                                for item in spool.iter_sorted(
                                    "validated",
                                    DataType.KLINES,
                                    resolved_request.base_timeframe,
                                )
                            )
                            plans = planned_by_stream[("derived", DataType.KLINES, target)]
                            derived_partitions = self._write_monthly(
                                dataset_id=dataset_id,
                                version=version,
                                layer="derived",
                                instrument=metadata.reference,
                                data_type=DataType.KLINES,
                                timeframe=target,
                                records=aggregate_klines_stream(
                                    base_stream, target_timeframe=target
                                ),
                                source_ids=tuple(
                                    item.partition_id for item in base_kline_partitions
                                ),
                                format_name=format_name,
                                compression=compression,
                            )
                            self._assert_publication_plan(plans, derived_partitions)
                            if not derived_partitions:
                                warnings.append(
                                    f"no complete groups produced for derived timeframe {target}"
                                )
                                continue
                            all_partitions.extend(derived_partitions)
                else:
                    machine.transition(RunState.HASHING)

                if machine.state is RunState.BUILDING_DERIVED:
                    machine.transition(RunState.HASHING)
                if not all_partitions:
                    raise StructuredIngestionError(
                        "NO_VALIDATED_PARTITIONS",
                        "no validated historical stream was available for publication",
                        False,
                        "hashing",
                    )

                self._assert_partition_non_overlap(all_partitions)
                actual_ordered_partitions = tuple(
                    sorted(
                        all_partitions,
                        key=lambda item: (
                            item.data_type.value,
                            item.timeframe or "",
                            item.start,
                            item.end,
                            item.partition_id,
                        ),
                    )
                )
                if actual_ordered_partitions != manifest.partitions:
                    raise StructuredIngestionError(
                        "PUBLICATION_PLAN_DRIFT",
                        "actual dataset partition set differed from the pre-publication manifest",
                        False,
                        "storage",
                        {
                            "planned_partition_count": len(manifest.partitions),
                            "actual_partition_count": len(actual_ordered_partitions),
                        },
                    )

                digest = dataset_hash(manifest)
                manifest_path = self._storage.write_manifest(dataset_id, version, manifest)
                if manifest_path.stat().st_size != manifest_byte_size:
                    raise StructuredIngestionError(
                        "PUBLICATION_PLAN_DRIFT",
                        "persisted manifest size differed from the pre-publication plan",
                        False,
                        "storage",
                        {
                            "planned_manifest_bytes": manifest_byte_size,
                            "actual_manifest_bytes": manifest_path.stat().st_size,
                        },
                    )
                descriptor = DatasetDescriptor(
                    dataset_id=dataset_id,
                    version=version,
                    dataset_hash=digest,
                    manifest_uri=manifest_path.relative_to(self._storage.root).as_posix(),
                    storage_uri=self._storage.root.as_uri(),
                    exchange_id=metadata.reference.exchange_id,
                    market_type=metadata.reference.market_type,
                    instrument_id=metadata.reference.canonical_id,
                    data_types=manifest.data_types,
                    timeframes=manifest.timeframes,
                    start=manifest.start,
                    end=manifest.end,
                    schema_version=manifest.schema_version,
                    validation_status=full_validation.status,
                    unresolved_issues=unresolved_codes,
                    lineage=manifest.lineage,
                )
                machine.transition(RunState.REGISTERING)
                self._registry.register_for_request(
                    descriptor,
                    request_id=request.request_id,
                    request_hash=execution_hash,
                )
                target = RunState.COMPLETED_WITH_WARNINGS if warnings else RunState.COMPLETED
                machine.transition(target)
                self._storage.clear_cancel(run_id)
                self._write_checkpoint(
                    run_id,
                    execution_hash,
                    machine.state,
                    completed_chunks,
                    pending_chunks,
                )
                return IngestionRunResult(
                    run_id=run_id,
                    final_state=machine.state,
                    dataset=descriptor,
                    validation=full_validation,
                    warnings=tuple(warnings),
                    completed_at=datetime.now(UTC),
                )

    def _cancelled_result(
        self,
        *,
        machine: IngestionStateMachine,
        run_id: str,
        execution_hash: str,
        completed_chunks: list[str],
        pending_chunks: list[str],
        warnings: list[str],
    ) -> IngestionRunResult | None:
        if not self._storage.is_cancel_requested(run_id):
            return None
        machine.transition(RunState.CANCELLED)
        self._write_checkpoint(
            run_id,
            execution_hash,
            machine.state,
            completed_chunks,
            pending_chunks,
        )
        return IngestionRunResult(
            run_id=run_id,
            final_state=RunState.CANCELLED,
            dataset=None,
            validation=None,
            warnings=tuple((*warnings, "cancellation requested by user")),
            completed_at=datetime.now(UTC),
        )

    def _write_checkpoint(
        self,
        run_id: str,
        request_hash: str,
        state: RunState,
        completed: list[str],
        pending: list[str],
    ) -> Path:
        checkpoint = DownloadCheckpoint(
            run_id=run_id,
            request_hash=request_hash,
            state=state,
            completed_chunks=tuple(completed),
            pending_chunks=tuple(pending),
            updated_at=datetime.now(UTC),
        )
        return self._storage.write_checkpoint(run_id, checkpoint)

    def _write_monthly(
        self,
        *,
        dataset_id: str,
        version: int,
        layer: str,
        instrument: InstrumentReference,
        data_type: DataType,
        timeframe: str | None,
        records: Iterable[NormalizedRecord],
        source_ids: tuple[str, ...],
        format_name: str,
        compression: str,
    ) -> tuple[NormalizedPartitionDescriptor, ...]:
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

        output: list[NormalizedPartitionDescriptor] = []
        for _, group in groupby(
            ordered_records(),
            key=lambda item: (item.timestamp.year, item.timestamp.month),
        ):
            output.append(
                self._storage.write_partition(
                    dataset_id=dataset_id,
                    version=version,
                    layer=layer,
                    instrument=instrument,
                    data_type=data_type,
                    timeframe=timeframe,
                    records=group,
                    source_artifact_ids=source_ids,
                    format_name=format_name,
                    compression=compression,
                )
            )
        return tuple(output)

    @staticmethod
    def _assert_publication_plan(
        planned: tuple[PartitionPublicationPlan, ...],
        actual: tuple[NormalizedPartitionDescriptor, ...],
    ) -> None:
        if len(actual) != len(planned):
            raise StructuredIngestionError(
                "PUBLICATION_PLAN_DRIFT",
                "actual partition count differed from the pre-publication plan",
                False,
                "storage",
                {
                    "planned_partition_count": len(planned),
                    "actual_partition_count": len(actual),
                },
            )
        for actual_partition, planned_partition in zip(actual, planned, strict=True):
            assert_publication_matches_plan(actual_partition, planned_partition)

    @staticmethod
    def _assert_partition_non_overlap(
        partitions: Iterable[NormalizedPartitionDescriptor],
    ) -> None:
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
        previous: NormalizedPartitionDescriptor | None = None
        for item in ordered:
            same_stream = (
                previous is not None
                and previous.data_type is item.data_type
                and previous.timeframe == item.timeframe
            )
            if same_stream and previous is not None and item.start < previous.end:
                raise StructuredIngestionError(
                    "PARTITION_RANGE_CONFLICT",
                    "new dataset version would contain overlapping partitions",
                    False,
                    "hashing",
                    {
                        "previous": previous.partition_id,
                        "current": item.partition_id,
                        "data_type": item.data_type.value,
                        "timeframe": item.timeframe,
                    },
                )
            previous = item

    @staticmethod
    def _merge_data_type_reports(
        reports: Iterable[DataTypeValidationReport],
    ) -> tuple[DataTypeValidationReport, ...]:
        grouped: dict[tuple[DataType, str | None], list[DataValidationReport]] = defaultdict(list)
        for item in reports:
            grouped[(item.data_type, item.timeframe)].append(item.report)
        return tuple(
            DataTypeValidationReport(
                data_type=data_type,
                timeframe=timeframe,
                report=combine_validation_reports(items),
            )
            for (data_type, timeframe), items in sorted(
                grouped.items(), key=lambda item: (item[0][0].value, item[0][1] or "")
            )
        )

    @staticmethod
    def _timeframe_for(data_type: DataType, base_timeframe: str) -> str | None:
        if data_type in _POINT_EVENT_TYPES:
            return None
        if data_type in _INTERVAL_TYPES:
            return base_timeframe
        return None

    @staticmethod
    def _timeframe_sort_key(value: str) -> tuple[int, str]:
        from .timeframes import timeframe_seconds

        return timeframe_seconds(value), value

    @staticmethod
    def _validate_acquired_chunk(
        expected: DownloadChunk,
        observed: DownloadChunk,
    ) -> None:
        mismatches: dict[str, object] = {}
        for field_name in ("chunk_id", "data_type", "timeframe", "start", "end"):
            expected_value = getattr(expected, field_name)
            observed_value = getattr(observed, field_name)
            if observed_value != expected_value:
                mismatches[field_name] = {
                    "expected": str(expected_value),
                    "observed": str(observed_value),
                }
        if mismatches:
            raise AdapterError(
                "ACQUIRED_CHUNK_MISMATCH",
                "adapter output does not match the planned chunk identity",
                False,
                "download",
                mismatches,
            )

    @staticmethod
    def _dataset_id(request: MarketDataRequest) -> str:
        market = request.market_type.value.replace("_", "-")
        return f"{request.exchange_id}_{market}_{request.symbol.lower()}_{request.base_timeframe}"
