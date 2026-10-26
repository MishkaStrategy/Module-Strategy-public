from __future__ import annotations

import argparse
import json
import re
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from .adapters.binance import (
    _FUNDING_MAX_POSITIVE_JITTER_MS,
    _FUNDING_TIMESTAMP_POLICY,
    BinanceAdapter,
)
from .contracts import DatasetDescriptor, DatasetManifest, NormalizedPartitionDescriptor
from .enums import DataType
from .errors import AdapterError
from .funding_dataset_reader import (
    RegisteredFundingSelection,
    iter_registered_funding,
    resolve_registered_funding_dataset,
)
from .hashing import dataset_hash
from .manifest_io import load_manifest, manifest_from_dict
from .price_dataset_reader import (
    RegisteredPriceSelection,
    iter_registered_prices,
    resolve_registered_price_dataset,
)
from .records import FundingRateRecord
from .registry import SQLiteDatasetRegistry, descriptor_from_dict
from .required_gap_policy import (
    RequiredGapPolicy,
    load_required_gap_policy,
    policy_adapter_version,
    policy_hash_from_adapter_version,
)
from .serialization import read_json
from .storage import LocalStorage

_DATASET_ID = "binance_usdt-m-perpetual_btcusdt_5m"
_PARENT_VERSION = 4
_TARGET_VERSION = 6
_PARENT_HASH = "fbbe9091d70ff4462a08e9fe650f73efbcc59373391c6701004ebbd1dc77868a"
_EXPECTED_FUNDING_EVENTS = 7_306
_EXPECTED_MARK_ROWS = 701_336
_REQUIRED_GAP_POLICY_HASH = "2ec4189035fc3d65d09c71e8894dbd6b438b2e4aabacad77f9d57b318670fc36"
_MARK_TIMEFRAME = "5m"
_SHA40 = re.compile(r"^[0-9a-f]{40}$")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class FundingV5MaterializationError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _safe_payload(*, status: str, code: str | None = None, **values: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "dataset_id": _DATASET_ID,
        "parent_version": _PARENT_VERSION,
        "target_version": _TARGET_VERSION,
        "parent_hash": _PARENT_HASH,
        "normalization_policy": _FUNDING_TIMESTAMP_POLICY,
        "approved_positive_jitter_ms_max": _FUNDING_MAX_POSITIVE_JITTER_MS,
        "raw_market_data_emitted": False,
        "private_path_emitted": False,
    }
    if code is not None:
        payload["code"] = code
    payload.update(values)
    return payload


def _verify_checkout(expected_sha: str) -> None:
    if _SHA40.fullmatch(expected_sha) is None:
        raise FundingV5MaterializationError("EXPECTED_SHA_INVALID")
    try:
        head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        dirty = subprocess.check_output(
            ["git", "status", "--porcelain"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise FundingV5MaterializationError("CHECKOUT_PROOF_UNAVAILABLE") from exc
    if head != expected_sha:
        raise FundingV5MaterializationError("CHECKOUT_SHA_MISMATCH")
    if dirty:
        raise FundingV5MaterializationError("CHECKOUT_NOT_CLEAN")


def _owner_root(path: Path) -> Path:
    candidate = path.expanduser()
    if candidate.is_symlink():
        raise FundingV5MaterializationError("OWNER_ROOT_UNSAFE")
    try:
        root = candidate.resolve(strict=True)
    except OSError as exc:
        raise FundingV5MaterializationError("OWNER_ROOT_UNAVAILABLE") from exc
    if not root.is_dir():
        raise FundingV5MaterializationError("OWNER_ROOT_UNSAFE")
    registry = root / "registry.sqlite3"
    if registry.is_symlink() or not registry.is_file():
        raise FundingV5MaterializationError("OWNER_REGISTRY_UNAVAILABLE")
    return root


def _epoch_milliseconds(timestamp: datetime) -> int:
    value = timestamp.astimezone(UTC)
    if value.microsecond % 1_000 != 0:
        raise FundingV5MaterializationError("PARENT_FUNDING_PRECISION_UNSUPPORTED")
    delta = value - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _canonicalize_record(record: FundingRateRecord) -> tuple[FundingRateRecord, int]:
    try:
        canonical, jitter_ms = BinanceAdapter._canonical_funding_time(
            _epoch_milliseconds(record.funding_time)
        )
    except (AdapterError, TypeError, ValueError) as exc:
        raise FundingV5MaterializationError("PARENT_FUNDING_BOUNDARY_UNMAPPABLE") from exc
    return replace(record, funding_time=canonical), jitter_ms


def _materialization_mode(*, target_exists: bool, next_version: int, orphan_exists: bool) -> str:
    if target_exists:
        if next_version != _TARGET_VERSION + 1:
            raise FundingV5MaterializationError("OWNER_VERSION_GEOMETRY_MISMATCH")
        return "verify"
    if next_version != _TARGET_VERSION:
        raise FundingV5MaterializationError("OWNER_VERSION_GEOMETRY_MISMATCH")
    return "resume_orphan" if orphan_exists else "materialize"


def _target_manifest_path(root: Path) -> Path:
    return root / "manifests" / _DATASET_ID / f"v{_TARGET_VERSION}.json"


def _partition_sort_key(
    item: NormalizedPartitionDescriptor,
) -> tuple[str, str, datetime, datetime, str]:
    return (
        item.data_type.value,
        item.timeframe or "",
        item.start,
        item.end,
        item.partition_id,
    )


def _verify_parent_data_types(data_types: tuple[DataType, ...]) -> None:
    required = {DataType.FUNDING_RATE, DataType.MARK_PRICE}
    if not required.issubset(data_types):
        raise FundingV5MaterializationError("PARENT_DATA_SHAPE_MISMATCH")


def _load_parent(
    root: Path,
) -> tuple[
    DatasetDescriptor,
    DatasetManifest,
    RegisteredFundingSelection,
    RegisteredPriceSelection,
    RequiredGapPolicy,
]:
    registry = SQLiteDatasetRegistry(root / "registry.sqlite3")
    payload = registry.inspect(_DATASET_ID, _PARENT_VERSION)
    if payload is None:
        raise FundingV5MaterializationError("PARENT_DATASET_MISSING")
    try:
        descriptor = descriptor_from_dict(payload)
    except (TypeError, ValueError) as exc:
        raise FundingV5MaterializationError("PARENT_DESCRIPTOR_INVALID") from exc
    if descriptor.dataset_hash != _PARENT_HASH:
        raise FundingV5MaterializationError("PARENT_HASH_MISMATCH")
    _verify_parent_data_types(descriptor.data_types)

    try:
        funding = resolve_registered_funding_dataset(
            root,
            _DATASET_ID,
            _PARENT_VERSION,
            consumer="strategy_factory",
            deep=True,
        )
        marks = resolve_registered_price_dataset(
            root,
            _DATASET_ID,
            _PARENT_VERSION,
            price_type=DataType.MARK_PRICE,
            timeframe=_MARK_TIMEFRAME,
            consumer="strategy_factory",
            deep=True,
        )
        manifest = load_manifest(root, descriptor)
    except Exception as exc:
        raise FundingV5MaterializationError("PARENT_DEEP_VERIFY_FAILED") from exc

    if (
        funding.descriptor.dataset_hash != _PARENT_HASH
        or marks.descriptor.dataset_hash != _PARENT_HASH
    ):
        raise FundingV5MaterializationError("PARENT_READER_HASH_MISMATCH")
    if funding.event_count != _EXPECTED_FUNDING_EVENTS:
        raise FundingV5MaterializationError("PARENT_FUNDING_COUNT_MISMATCH")
    if marks.row_count != _EXPECTED_MARK_ROWS:
        raise FundingV5MaterializationError("PARENT_MARK_COUNT_MISMATCH")

    try:
        policy_hash = policy_hash_from_adapter_version(manifest.exchange.adapter_version)
    except ValueError as exc:
        raise FundingV5MaterializationError("PARENT_POLICY_IDENTITY_INVALID") from exc
    if policy_hash != _REQUIRED_GAP_POLICY_HASH:
        raise FundingV5MaterializationError("PARENT_POLICY_IDENTITY_MISMATCH")
    try:
        policy = load_required_gap_policy(root, policy_hash)
    except (OSError, TypeError, ValueError) as exc:
        raise FundingV5MaterializationError("PARENT_POLICY_SIDECAR_INVALID") from exc
    if policy.canonical_hash != _REQUIRED_GAP_POLICY_HASH:
        raise FundingV5MaterializationError("PARENT_POLICY_SIDECAR_MISMATCH")
    _verify_sparse_mark_stream(marks, policy)
    return descriptor, manifest, funding, marks, policy


def _verify_sparse_mark_stream(
    marks: RegisteredPriceSelection,
    policy: RequiredGapPolicy,
) -> None:
    observed = 0
    cursor = marks.partition_start_inclusive
    for exception in policy.exceptions:
        if exception.start < cursor or exception.end > marks.partition_end_exclusive:
            raise FundingV5MaterializationError("MARK_GAP_BOUNDS_INVALID")
        if cursor < exception.start:
            observed += sum(
                1
                for _ in iter_registered_prices(
                    marks,
                    start=cursor,
                    end=exception.start,
                )
            )
        cursor = exception.end
    if cursor < marks.partition_end_exclusive:
        observed += sum(
            1
            for _ in iter_registered_prices(
                marks,
                start=cursor,
                end=marks.partition_end_exclusive,
            )
        )
    if observed != marks.row_count or observed != _EXPECTED_MARK_ROWS:
        raise FundingV5MaterializationError("MARK_SPARSE_SHAPE_MISMATCH")


def _build_target_manifest(
    *,
    parent: DatasetManifest,
    parent_hash: str,
    funding_partitions: tuple[NormalizedPartitionDescriptor, ...],
    adapter_version: str,
    normalized_event_count: int,
    max_positive_jitter_ms: int,
    created_at: datetime,
) -> DatasetManifest:
    parent_funding = tuple(
        item for item in parent.partitions if item.data_type is DataType.FUNDING_RATE
    )
    parent_marks = tuple(
        item for item in parent.partitions if item.data_type is DataType.MARK_PRICE
    )
    parent_passthrough = tuple(
        item for item in parent.partitions if item.data_type is not DataType.FUNDING_RATE
    )
    if not parent_funding or not parent_marks:
        raise FundingV5MaterializationError("PARENT_PARTITION_SHAPE_INVALID")
    if len(funding_partitions) != len(parent_funding):
        raise FundingV5MaterializationError("TARGET_FUNDING_PARTITION_COUNT_MISMATCH")
    if any(item.data_type is not DataType.FUNDING_RATE for item in funding_partitions):
        raise FundingV5MaterializationError("TARGET_FUNDING_PARTITION_TYPE_MISMATCH")

    partitions = tuple(
        sorted(
            (*parent_passthrough, *funding_partitions),
            key=_partition_sort_key,
        )
    )
    return replace(
        parent,
        version=_TARGET_VERSION,
        exchange=replace(parent.exchange, adapter_version=adapter_version),
        partitions=partitions,
        lineage=(
            *parent.lineage,
            f"parent:{_DATASET_ID}:v{_PARENT_VERSION}:{parent_hash}",
            (
                f"funding-normalization:{_FUNDING_TIMESTAMP_POLICY}:"
                f"normalized={normalized_event_count}:"
                f"max-positive-jitter-ms={max_positive_jitter_ms}"
            ),
            f"mark-price-reused:{_DATASET_ID}:v{_PARENT_VERSION}:{parent_hash}",
        ),
        created_at=created_at,
    )


def _descriptor_for_manifest(
    *,
    root: Path,
    parent: DatasetDescriptor,
    manifest: DatasetManifest,
) -> DatasetDescriptor:
    digest = dataset_hash(manifest)
    manifest_uri = _target_manifest_path(root).relative_to(root).as_posix()
    return replace(
        parent,
        version=_TARGET_VERSION,
        dataset_hash=digest,
        manifest_uri=manifest_uri,
        lineage=manifest.lineage,
    )


def _rebuild_funding_partitions(
    *,
    storage: LocalStorage,
    parent: DatasetManifest,
) -> tuple[tuple[NormalizedPartitionDescriptor, ...], int, int]:
    source_partitions = tuple(
        sorted(
            (item for item in parent.partitions if item.data_type is DataType.FUNDING_RATE),
            key=lambda item: (item.start, item.end, item.partition_id),
        )
    )
    if not source_partitions:
        raise FundingV5MaterializationError("PARENT_FUNDING_PARTITIONS_MISSING")

    month_keys: set[tuple[int, int]] = set()
    replacements: list[NormalizedPartitionDescriptor] = []
    normalized_event_count = 0
    max_positive_jitter_ms = 0
    previous_timestamp: datetime | None = None
    observed_events = 0

    for partition in source_partitions:
        key = (partition.start.year, partition.start.month)
        if key in month_keys:
            raise FundingV5MaterializationError("PARENT_FUNDING_MONTH_DUPLICATE")
        month_keys.add(key)
        records: list[FundingRateRecord] = []
        for raw_record in storage.iter_partition_records(partition):
            if not isinstance(raw_record, FundingRateRecord):
                raise FundingV5MaterializationError("PARENT_FUNDING_RECORD_TYPE_INVALID")
            record, jitter_ms = _canonicalize_record(raw_record)
            if previous_timestamp is not None and record.funding_time <= previous_timestamp:
                raise FundingV5MaterializationError("TARGET_FUNDING_TIMESTAMP_COLLISION")
            previous_timestamp = record.funding_time
            observed_events += 1
            if jitter_ms:
                normalized_event_count += 1
                max_positive_jitter_ms = max(max_positive_jitter_ms, jitter_ms)
            records.append(record)
        if len(records) != partition.row_count:
            raise FundingV5MaterializationError("PARENT_FUNDING_PARTITION_COUNT_MISMATCH")
        replacement = storage.write_partition(
            dataset_id=_DATASET_ID,
            version=_TARGET_VERSION,
            layer="normalized",
            instrument=parent.instrument,
            data_type=DataType.FUNDING_RATE,
            timeframe=None,
            records=records,
            source_artifact_ids=partition.source_artifact_ids,
            format_name=partition.format,
            compression="zstd",
        )
        if replacement.row_count != partition.row_count:
            raise FundingV5MaterializationError("TARGET_FUNDING_PARTITION_COUNT_MISMATCH")
        if replacement.source_artifact_ids != partition.source_artifact_ids:
            raise FundingV5MaterializationError("TARGET_FUNDING_LINEAGE_MISMATCH")
        replacements.append(replacement)

    if observed_events != _EXPECTED_FUNDING_EVENTS:
        raise FundingV5MaterializationError("TARGET_FUNDING_COUNT_MISMATCH")
    if normalized_event_count <= 0:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_EMPTY")
    if max_positive_jitter_ms > _FUNDING_MAX_POSITIVE_JITTER_MS:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_WINDOW_EXCEEDED")
    return tuple(replacements), normalized_event_count, max_positive_jitter_ms


def _load_orphan_manifest(root: Path) -> DatasetManifest:
    path = _target_manifest_path(root)
    if path.is_symlink() or not path.is_file():
        raise FundingV5MaterializationError("TARGET_ORPHAN_MANIFEST_UNAVAILABLE")
    try:
        payload = read_json(path)
        manifest = manifest_from_dict(payload)
    except Exception as exc:
        raise FundingV5MaterializationError("TARGET_ORPHAN_MANIFEST_INVALID") from exc
    if manifest.dataset_id != _DATASET_ID or manifest.version != _TARGET_VERSION:
        raise FundingV5MaterializationError("TARGET_ORPHAN_MANIFEST_IDENTITY_MISMATCH")
    return manifest


def _verify_candidate_files_against_parent(
    *,
    root: Path,
    parent_manifest: DatasetManifest,
    target_manifest: DatasetManifest,
) -> tuple[int, int]:
    storage = LocalStorage(root)
    parent_partitions = tuple(
        sorted(
            (
                item
                for item in parent_manifest.partitions
                if item.data_type is DataType.FUNDING_RATE
            ),
            key=lambda item: (item.start, item.end, item.partition_id),
        )
    )
    target_partitions = tuple(
        sorted(
            (
                item
                for item in target_manifest.partitions
                if item.data_type is DataType.FUNDING_RATE
            ),
            key=lambda item: (item.start, item.end, item.partition_id),
        )
    )
    if len(parent_partitions) != len(target_partitions):
        raise FundingV5MaterializationError("TARGET_FUNDING_PARTITION_COUNT_MISMATCH")

    normalized_event_count = 0
    max_positive_jitter_ms = 0
    observed_events = 0
    previous_timestamp: datetime | None = None
    for parent_partition, target_partition in zip(
        parent_partitions,
        target_partitions,
        strict=True,
    ):
        if target_partition.source_artifact_ids != parent_partition.source_artifact_ids:
            raise FundingV5MaterializationError("TARGET_FUNDING_LINEAGE_MISMATCH")
        if target_partition.row_count != parent_partition.row_count:
            raise FundingV5MaterializationError("TARGET_FUNDING_PARTITION_COUNT_MISMATCH")
        parent_records = tuple(storage.iter_partition_records(parent_partition))
        target_records = tuple(storage.iter_partition_records(target_partition))
        if len(parent_records) != len(target_records):
            raise FundingV5MaterializationError("TARGET_FUNDING_PARTITION_COUNT_MISMATCH")
        for parent_record, target_record in zip(parent_records, target_records, strict=True):
            if not isinstance(parent_record, FundingRateRecord) or not isinstance(
                target_record, FundingRateRecord
            ):
                raise FundingV5MaterializationError("TARGET_FUNDING_RECORD_TYPE_INVALID")
            expected, jitter_ms = _canonicalize_record(parent_record)
            if target_record != expected:
                raise FundingV5MaterializationError("TARGET_FUNDING_ECONOMICS_OR_TIME_MISMATCH")
            if previous_timestamp is not None and target_record.funding_time <= previous_timestamp:
                raise FundingV5MaterializationError("TARGET_FUNDING_TIMESTAMP_COLLISION")
            previous_timestamp = target_record.funding_time
            observed_events += 1
            if jitter_ms:
                normalized_event_count += 1
                max_positive_jitter_ms = max(max_positive_jitter_ms, jitter_ms)

    if observed_events != _EXPECTED_FUNDING_EVENTS:
        raise FundingV5MaterializationError("TARGET_FUNDING_COUNT_MISMATCH")
    if normalized_event_count <= 0:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_EMPTY")
    if max_positive_jitter_ms > _FUNDING_MAX_POSITIVE_JITTER_MS:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_WINDOW_EXCEEDED")
    return normalized_event_count, max_positive_jitter_ms


def _verify_target_against_parent(
    *,
    root: Path,
    parent_descriptor: DatasetDescriptor,
    parent_manifest: DatasetManifest,
    parent_funding: RegisteredFundingSelection,
    parent_marks: RegisteredPriceSelection,
    policy: RequiredGapPolicy,
    target_descriptor: DatasetDescriptor,
    target_manifest: DatasetManifest,
) -> tuple[int, int]:
    if target_descriptor.version != _TARGET_VERSION:
        raise FundingV5MaterializationError("TARGET_VERSION_MISMATCH")
    if target_manifest.version != _TARGET_VERSION:
        raise FundingV5MaterializationError("TARGET_MANIFEST_VERSION_MISMATCH")
    if target_descriptor.dataset_hash != dataset_hash(target_manifest):
        raise FundingV5MaterializationError("TARGET_HASH_MISMATCH")
    expected_descriptor = _descriptor_for_manifest(
        root=root,
        parent=parent_descriptor,
        manifest=target_manifest,
    )
    if target_descriptor != expected_descriptor:
        raise FundingV5MaterializationError("TARGET_DESCRIPTOR_CONTRACT_DRIFT")
    _verify_candidate_without_registry(
        root=root,
        parent_manifest=parent_manifest,
        target_manifest=target_manifest,
        policy=policy,
    )
    expected_adapter_version = policy_adapter_version(BinanceAdapter.adapter_version, policy)
    if target_manifest.exchange.adapter_version != expected_adapter_version:
        raise FundingV5MaterializationError("TARGET_ADAPTER_IDENTITY_MISMATCH")
    if target_manifest.source_artifacts != parent_manifest.source_artifacts:
        raise FundingV5MaterializationError("TARGET_RAW_LINEAGE_MUTATED")

    parent_mark_partitions = tuple(
        item for item in parent_manifest.partitions if item.data_type is DataType.MARK_PRICE
    )
    target_mark_partitions = tuple(
        item for item in target_manifest.partitions if item.data_type is DataType.MARK_PRICE
    )
    if target_mark_partitions != parent_mark_partitions:
        raise FundingV5MaterializationError("TARGET_MARK_PARTITIONS_MUTATED")

    try:
        target_funding = resolve_registered_funding_dataset(
            root,
            _DATASET_ID,
            _TARGET_VERSION,
            consumer="strategy_factory",
            deep=True,
        )
        target_marks = resolve_registered_price_dataset(
            root,
            _DATASET_ID,
            _TARGET_VERSION,
            price_type=DataType.MARK_PRICE,
            timeframe=_MARK_TIMEFRAME,
            consumer="strategy_factory",
            deep=True,
        )
    except Exception as exc:
        raise FundingV5MaterializationError("TARGET_DEEP_VERIFY_FAILED") from exc

    if target_funding.descriptor.dataset_hash != target_marks.descriptor.dataset_hash:
        raise FundingV5MaterializationError("TARGET_READER_HASH_SPLIT")
    if target_marks.partitions != parent_marks.partitions:
        raise FundingV5MaterializationError("TARGET_MARK_SELECTION_MUTATED")
    if target_marks.row_count != _EXPECTED_MARK_ROWS:
        raise FundingV5MaterializationError("TARGET_MARK_COUNT_MISMATCH")
    if target_funding.event_count != _EXPECTED_FUNDING_EVENTS:
        raise FundingV5MaterializationError("TARGET_FUNDING_COUNT_MISMATCH")
    if target_funding.source_artifacts != parent_funding.source_artifacts:
        raise FundingV5MaterializationError("TARGET_FUNDING_RAW_LINEAGE_MUTATED")
    if target_marks.source_artifacts != parent_marks.source_artifacts:
        raise FundingV5MaterializationError("TARGET_MARK_RAW_LINEAGE_MUTATED")
    _verify_sparse_mark_stream(target_marks, policy)

    parent_events = tuple(iter_registered_funding(parent_funding))
    target_events = tuple(iter_registered_funding(target_funding))
    if len(parent_events) != len(target_events):
        raise FundingV5MaterializationError("TARGET_FUNDING_COUNT_MISMATCH")

    normalized_event_count = 0
    max_positive_jitter_ms = 0
    for parent_event, target_event in zip(parent_events, target_events, strict=True):
        expected, jitter_ms = _canonicalize_record(parent_event)
        if target_event != expected:
            raise FundingV5MaterializationError("TARGET_FUNDING_ECONOMICS_OR_TIME_MISMATCH")
        if target_event.funding_time.minute != 0 or target_event.funding_time.second != 0:
            raise FundingV5MaterializationError("TARGET_FUNDING_BOUNDARY_INVALID")
        if target_event.funding_time.microsecond != 0:
            raise FundingV5MaterializationError("TARGET_FUNDING_BOUNDARY_INVALID")
        if jitter_ms:
            normalized_event_count += 1
            max_positive_jitter_ms = max(max_positive_jitter_ms, jitter_ms)

    if normalized_event_count <= 0:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_EMPTY")
    if max_positive_jitter_ms > _FUNDING_MAX_POSITIVE_JITTER_MS:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_WINDOW_EXCEEDED")
    if parent_descriptor.dataset_hash != _PARENT_HASH:
        raise FundingV5MaterializationError("PARENT_HASH_DRIFT")
    return normalized_event_count, max_positive_jitter_ms


def _register_or_verify(root: Path) -> dict[str, object]:
    parent_descriptor, parent_manifest, parent_funding, parent_marks, policy = _load_parent(root)
    registry = SQLiteDatasetRegistry(root / "registry.sqlite3")
    target_payload = registry.inspect(_DATASET_ID, _TARGET_VERSION)
    orphan_exists = _target_manifest_path(root).is_file() and target_payload is None
    mode = _materialization_mode(
        target_exists=target_payload is not None,
        next_version=registry.next_version(_DATASET_ID),
        orphan_exists=orphan_exists,
    )

    if mode == "verify":
        if target_payload is None:
            raise FundingV5MaterializationError("TARGET_DESCRIPTOR_INVALID")
        try:
            target_descriptor = descriptor_from_dict(target_payload)
            target_manifest = load_manifest(root, target_descriptor)
        except Exception as exc:
            raise FundingV5MaterializationError("TARGET_DESCRIPTOR_INVALID") from exc
    elif mode == "resume_orphan":
        target_manifest = _load_orphan_manifest(root)
        target_descriptor = _descriptor_for_manifest(
            root=root,
            parent=parent_descriptor,
            manifest=target_manifest,
        )
        _verify_candidate_without_registry(
            root=root,
            parent_manifest=parent_manifest,
            target_manifest=target_manifest,
            policy=policy,
        )
        registry.register(target_descriptor)
    else:
        storage = LocalStorage(root)
        funding_partitions, normalized_count, max_jitter = _rebuild_funding_partitions(
            storage=storage,
            parent=parent_manifest,
        )
        target_manifest = _build_target_manifest(
            parent=parent_manifest,
            parent_hash=parent_descriptor.dataset_hash,
            funding_partitions=funding_partitions,
            adapter_version=policy_adapter_version(BinanceAdapter.adapter_version, policy),
            normalized_event_count=normalized_count,
            max_positive_jitter_ms=max_jitter,
            created_at=datetime.now(UTC),
        )
        target_descriptor = _descriptor_for_manifest(
            root=root,
            parent=parent_descriptor,
            manifest=target_manifest,
        )
        _verify_candidate_without_registry(
            root=root,
            parent_manifest=parent_manifest,
            target_manifest=target_manifest,
            policy=policy,
        )
        storage.write_manifest(_DATASET_ID, _TARGET_VERSION, target_manifest)
        registry.register(target_descriptor)

    normalized_count, max_jitter = _verify_target_against_parent(
        root=root,
        parent_descriptor=parent_descriptor,
        parent_manifest=parent_manifest,
        parent_funding=parent_funding,
        parent_marks=parent_marks,
        policy=policy,
        target_descriptor=target_descriptor,
        target_manifest=target_manifest,
    )
    return _safe_payload(
        status="ok",
        mode=mode,
        dataset_hash=target_descriptor.dataset_hash,
        funding_event_count=_EXPECTED_FUNDING_EVENTS,
        mark_price_row_count=_EXPECTED_MARK_ROWS,
        normalized_funding_event_count=normalized_count,
        max_positive_jitter_ms=max_jitter,
        policy_hash=_REQUIRED_GAP_POLICY_HASH,
        mark_partitions_reused=True,
        raw_lineage_reused=True,
        deep_verified=True,
    )


def _verify_candidate_without_registry(
    *,
    root: Path,
    parent_manifest: DatasetManifest,
    target_manifest: DatasetManifest,
    policy: RequiredGapPolicy,
) -> tuple[int, int]:
    if target_manifest.dataset_id != parent_manifest.dataset_id:
        raise FundingV5MaterializationError("TARGET_ORPHAN_DATASET_MISMATCH")
    if target_manifest.version != _TARGET_VERSION:
        raise FundingV5MaterializationError("TARGET_ORPHAN_VERSION_MISMATCH")
    if target_manifest.source_artifacts != parent_manifest.source_artifacts:
        raise FundingV5MaterializationError("TARGET_RAW_LINEAGE_MUTATED")
    expected_adapter = policy_adapter_version(BinanceAdapter.adapter_version, policy)
    if target_manifest.exchange.adapter_version != expected_adapter:
        raise FundingV5MaterializationError("TARGET_ADAPTER_IDENTITY_MISMATCH")
    parent_marks = tuple(
        item for item in parent_manifest.partitions if item.data_type is DataType.MARK_PRICE
    )
    target_marks = tuple(
        item for item in target_manifest.partitions if item.data_type is DataType.MARK_PRICE
    )
    if target_marks != parent_marks:
        raise FundingV5MaterializationError("TARGET_MARK_PARTITIONS_MUTATED")
    parent_passthrough = tuple(
        sorted(
            (
                item
                for item in parent_manifest.partitions
                if item.data_type is not DataType.FUNDING_RATE
            ),
            key=_partition_sort_key,
        )
    )
    target_passthrough = tuple(
        sorted(
            (
                item
                for item in target_manifest.partitions
                if item.data_type is not DataType.FUNDING_RATE
            ),
            key=_partition_sort_key,
        )
    )
    if target_passthrough != parent_passthrough:
        raise FundingV5MaterializationError("TARGET_NON_FUNDING_PARTITIONS_MUTATED")
    lineage = set(target_manifest.lineage)
    expected_parent = f"parent:{_DATASET_ID}:v{_PARENT_VERSION}:{_PARENT_HASH}"
    if expected_parent not in lineage:
        raise FundingV5MaterializationError("TARGET_PARENT_LINEAGE_MISSING")
    normalized_count, max_jitter = _verify_candidate_files_against_parent(
        root=root,
        parent_manifest=parent_manifest,
        target_manifest=target_manifest,
    )
    expected_normalization_lineage = (
        f"funding-normalization:{_FUNDING_TIMESTAMP_POLICY}:"
        f"normalized={normalized_count}:"
        f"max-positive-jitter-ms={max_jitter}"
    )
    if expected_normalization_lineage not in lineage:
        raise FundingV5MaterializationError("TARGET_NORMALIZATION_LINEAGE_MISMATCH")
    expected_manifest = _build_target_manifest(
        parent=parent_manifest,
        parent_hash=_PARENT_HASH,
        funding_partitions=tuple(
            item for item in target_manifest.partitions if item.data_type is DataType.FUNDING_RATE
        ),
        adapter_version=expected_adapter,
        normalized_event_count=normalized_count,
        max_positive_jitter_ms=max_jitter,
        created_at=target_manifest.created_at,
    )
    if target_manifest != expected_manifest:
        raise FundingV5MaterializationError("TARGET_MANIFEST_CONTRACT_DRIFT")
    return normalized_count, max_jitter


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Materialize or verify immutable MS5A funding+MARK_PRICE owner v6"
    )
    parser.add_argument("--mode", choices=("check", "materialize"), required=True)
    parser.add_argument("--expected-sha", required=True)
    parser.add_argument("--storage-root", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        _verify_checkout(args.expected_sha)
        if BinanceAdapter.adapter_version != "0.5.0":
            raise FundingV5MaterializationError("ADAPTER_VERSION_MISMATCH")
        if _FUNDING_TIMESTAMP_POLICY != "binance-usdtm-hour-boundary-jitter-v2":
            raise FundingV5MaterializationError("NORMALIZATION_POLICY_MISMATCH")
        if _FUNDING_MAX_POSITIVE_JITTER_MS != 47:
            raise FundingV5MaterializationError("NORMALIZATION_WINDOW_MISMATCH")
        if args.mode == "check":
            print(
                json.dumps(
                    _safe_payload(
                        status="ok",
                        mode="check",
                        owner_mutation_performed=False,
                        expected_funding_event_count=_EXPECTED_FUNDING_EVENTS,
                        expected_mark_price_row_count=_EXPECTED_MARK_ROWS,
                        policy_hash=_REQUIRED_GAP_POLICY_HASH,
                    ),
                    sort_keys=True,
                    indent=2,
                )
            )
            return 0
        if args.storage_root is None:
            raise FundingV5MaterializationError("OWNER_ROOT_REQUIRED")
        root = _owner_root(args.storage_root)
        print(json.dumps(_register_or_verify(root), sort_keys=True, indent=2))
        return 0
    except FundingV5MaterializationError as exc:
        print(
            json.dumps(
                _safe_payload(status="error", code=exc.code, retryable=False),
                sort_keys=True,
                indent=2,
            )
        )
        return 1
    except Exception as exc:
        print(
            json.dumps(
                _safe_payload(
                    status="error",
                    code="UNEXPECTED_OPERATOR_FAILURE",
                    retryable=False,
                    error_type=type(exc).__name__,
                ),
                sort_keys=True,
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
