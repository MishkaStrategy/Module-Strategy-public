from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

import pytest

from market_data_ingestion.contracts import (
    DatasetManifest,
    DataValidationReport,
    ExchangeReference,
    InstrumentReference,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
)
from market_data_ingestion.enums import DataType, MarketType, SourceKind, ValidationStatus
from market_data_ingestion.funding_v5_materialization import (
    _PARENT_HASH,
    FundingV5MaterializationError,
    _build_target_manifest,
    _canonicalize_record,
    _materialization_mode,
    _verify_candidate_without_registry,
    _verify_parent_data_types,
)
from market_data_ingestion.records import FundingRateRecord
from market_data_ingestion.required_gap_policy import RequiredGapPolicy
from market_data_ingestion.storage import LocalStorage


def _record(timestamp: datetime, *, rate: str = "0.0001") -> FundingRateRecord:
    return FundingRateRecord(
        funding_time=timestamp,
        funding_rate=rate,
        mark_price="37000",
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
        source_kind="official_rest",
    )


def _raw(
    artifact_id: str,
    *,
    start: datetime,
    end: datetime,
    count: int,
) -> RawArtifactDescriptor:
    return RawArtifactDescriptor(
        artifact_id=artifact_id,
        source=SourceKind.OFFICIAL_REST,
        source_uri="https://fapi.binance.com",
        retrieved_at=start,
        checksum_sha256=("a" if artifact_id.endswith("funding") else "b") * 64,
        byte_size=128,
        record_count=count,
        start=start,
        end=end,
        adapter_version="0.3.0",
        response_metadata={"records": count},
    )


def _mark_partition(start: datetime) -> NormalizedPartitionDescriptor:
    return NormalizedPartitionDescriptor(
        partition_id="part:" + "c" * 24,
        data_type=DataType.MARK_PRICE,
        timeframe="5m",
        storage_uri="normalized/v4/mark-price.parquet",
        checksum_sha256="c" * 64,
        row_count=1,
        start=start,
        end=start + timedelta(minutes=5),
        schema_version=1,
        source_artifact_ids=("raw-mark",),
    )


def _manifest(
    *,
    funding_partition: NormalizedPartitionDescriptor,
    start: datetime,
) -> DatasetManifest:
    end = start + timedelta(hours=8)
    return DatasetManifest(
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        version=4,
        exchange=ExchangeReference(
            id="binance",
            adapter_version="0.3.0+required-gap-policy.synthetic",
        ),
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument=InstrumentReference(
            exchange_id="binance",
            market_type=MarketType.USDT_M_PERPETUAL,
            symbol="BTCUSDT",
            canonical_id="binance:usdt_m_perpetual:btcusdt",
        ),
        data_types=(DataType.FUNDING_RATE, DataType.MARK_PRICE),
        timeframes=("5m",),
        start=start,
        end=end,
        partitions=(funding_partition, _mark_partition(start)),
        source_artifacts=(
            _raw("raw-funding", start=start, end=end, count=1),
            _raw("raw-mark", start=start, end=end, count=1),
        ),
        validation=DataValidationReport(
            status=ValidationStatus.VALID,
            issues=(),
            gaps=(),
            duplicates=(),
            rows_checked=2,
            created_at=start,
        ),
        normalization_version="1.3.0",
        validation_policy_version="1",
        schema_version=1,
        lineage=("synthetic-v4",),
        created_at=start,
    )


def _candidate(
    root: Path,
    *,
    target_rate: str = "0.0001",
) -> tuple[DatasetManifest, DatasetManifest]:
    storage = LocalStorage(root)
    raw_time = datetime(2023, 11, 12, 16, 0, 0, 1_000, tzinfo=UTC)
    parent_partition = storage.write_partition(
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        version=4,
        layer="normalized",
        instrument=InstrumentReference(
            exchange_id="binance",
            market_type=MarketType.USDT_M_PERPETUAL,
            symbol="BTCUSDT",
            canonical_id="binance:usdt_m_perpetual:btcusdt",
        ),
        data_type=DataType.FUNDING_RATE,
        timeframe=None,
        records=(_record(raw_time),),
        source_artifact_ids=("raw-funding",),
    )
    parent = _manifest(funding_partition=parent_partition, start=raw_time.replace(microsecond=0))

    target_partition = storage.write_partition(
        dataset_id="binance_usdt-m-perpetual_btcusdt_5m",
        version=6,
        layer="normalized",
        instrument=parent.instrument,
        data_type=DataType.FUNDING_RATE,
        timeframe=None,
        records=(_record(raw_time.replace(microsecond=0), rate=target_rate),),
        source_artifact_ids=("raw-funding",),
    )
    target = _build_target_manifest(
        parent=parent,
        parent_hash=_PARENT_HASH,
        funding_partitions=(target_partition,),
        adapter_version="adapter-v5",
        normalized_event_count=1,
        max_positive_jitter_ms=1,
        created_at=raw_time + timedelta(seconds=1),
    )
    return parent, target


def test_parent_data_shape_requires_funding_and_mark_but_allows_extra_types() -> None:
    _verify_parent_data_types((DataType.FUNDING_RATE, DataType.MARK_PRICE, DataType.INDEX_PRICE))
    with pytest.raises(FundingV5MaterializationError) as raised:
        _verify_parent_data_types((DataType.FUNDING_RATE, DataType.INDEX_PRICE))
    assert raised.value.code == "PARENT_DATA_SHAPE_MISMATCH"


def test_target_manifest_preserves_all_non_funding_partitions(tmp_path: Path) -> None:
    parent, target = _candidate(tmp_path)
    index_partition = replace(
        _mark_partition(parent.start),
        partition_id="part:" + "d" * 24,
        data_type=DataType.INDEX_PRICE,
        storage_uri="normalized/v4/index-price.parquet",
        checksum_sha256="d" * 64,
        source_artifact_ids=("raw-mark",),
    )
    extended_parent = replace(
        parent,
        data_types=(*parent.data_types, DataType.INDEX_PRICE),
        partitions=(*parent.partitions, index_partition),
    )
    target_funding = tuple(
        item for item in target.partitions if item.data_type is DataType.FUNDING_RATE
    )

    rebuilt = _build_target_manifest(
        parent=extended_parent,
        parent_hash=_PARENT_HASH,
        funding_partitions=target_funding,
        adapter_version=target.exchange.adapter_version,
        normalized_event_count=1,
        max_positive_jitter_ms=1,
        created_at=target.created_at,
    )

    assert index_partition in rebuilt.partitions
    assert tuple(
        item for item in rebuilt.partitions if item.data_type is not DataType.FUNDING_RATE
    ) == tuple(
        sorted(
            (
                item
                for item in extended_parent.partitions
                if item.data_type is not DataType.FUNDING_RATE
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


def test_materialization_mode_is_single_version_and_idempotent() -> None:
    assert (
        _materialization_mode(target_exists=True, next_version=7, orphan_exists=False) == "verify"
    )
    with pytest.raises(FundingV5MaterializationError) as newer:
        _materialization_mode(target_exists=True, next_version=8, orphan_exists=False)
    assert newer.value.code == "OWNER_VERSION_GEOMETRY_MISMATCH"
    assert _materialization_mode(target_exists=False, next_version=6, orphan_exists=False) == (
        "materialize"
    )
    assert _materialization_mode(target_exists=False, next_version=6, orphan_exists=True) == (
        "resume_orphan"
    )
    with pytest.raises(FundingV5MaterializationError) as raised:
        _materialization_mode(target_exists=False, next_version=7, orphan_exists=False)
    assert raised.value.code == "OWNER_VERSION_GEOMETRY_MISMATCH"


def test_operator_canonicalization_preserves_economics_and_fails_closed() -> None:
    boundary = datetime(2023, 11, 12, 16, 0, tzinfo=UTC)
    for approved_jitter_ms in (1, 17, 47):
        source = _record(boundary + timedelta(milliseconds=approved_jitter_ms))
        normalized, jitter_ms = _canonicalize_record(source)

        assert jitter_ms == approved_jitter_ms
        assert normalized.funding_time == boundary
        assert normalized.funding_rate == source.funding_rate
        assert normalized.mark_price == source.mark_price
        assert normalized.symbol == source.symbol

    with pytest.raises(FundingV5MaterializationError) as raised:
        _canonicalize_record(_record(boundary + timedelta(milliseconds=48)))
    assert raised.value.code == "PARENT_FUNDING_BOUNDARY_UNMAPPABLE"


def test_target_manifest_reuses_raw_and_mark_partitions() -> None:
    start = datetime(2023, 11, 12, 16, 0, tzinfo=UTC)
    parent_funding = NormalizedPartitionDescriptor(
        partition_id="part:" + "d" * 24,
        data_type=DataType.FUNDING_RATE,
        timeframe=None,
        storage_uri="normalized/v4/funding.parquet",
        checksum_sha256="d" * 64,
        row_count=1,
        start=start + timedelta(milliseconds=1),
        end=start + timedelta(milliseconds=1, microseconds=1),
        schema_version=1,
        source_artifact_ids=("raw-funding",),
    )
    target_funding = replace(
        parent_funding,
        partition_id="part:" + "e" * 24,
        storage_uri="normalized/v6/funding.parquet",
        checksum_sha256="e" * 64,
        start=start,
        end=start + timedelta(microseconds=1),
    )
    parent = _manifest(funding_partition=parent_funding, start=start)

    target = _build_target_manifest(
        parent=parent,
        parent_hash=_PARENT_HASH,
        funding_partitions=(target_funding,),
        adapter_version="adapter-v5",
        normalized_event_count=1,
        max_positive_jitter_ms=1,
        created_at=start + timedelta(seconds=1),
    )

    assert target.version == 6
    assert target.exchange.adapter_version == "adapter-v5"
    assert target.source_artifacts == parent.source_artifacts
    assert tuple(item for item in target.partitions if item.data_type is DataType.MARK_PRICE) == (
        _mark_partition(start),
    )
    assert tuple(item for item in target.partitions if item.data_type is DataType.FUNDING_RATE) == (
        target_funding,
    )
    assert (
        "funding-normalization:binance-usdtm-hour-boundary-jitter-v2:"
        "normalized=1:max-positive-jitter-ms=1"
    ) in target.lineage


def test_candidate_files_are_verified_before_registry_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, target = _candidate(tmp_path)
    monkeypatch.setattr(
        "market_data_ingestion.funding_v5_materialization._EXPECTED_FUNDING_EVENTS",
        1,
    )
    monkeypatch.setattr(
        "market_data_ingestion.funding_v5_materialization.policy_adapter_version",
        lambda _base, _policy: "adapter-v5",
    )

    normalized_count, max_jitter = _verify_candidate_without_registry(
        root=tmp_path,
        parent_manifest=parent,
        target_manifest=target,
        policy=cast(RequiredGapPolicy, object()),
    )

    assert normalized_count == 1
    assert max_jitter == 1


def test_candidate_economics_drift_fails_before_registry_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, target = _candidate(tmp_path, target_rate="0.0002")
    monkeypatch.setattr(
        "market_data_ingestion.funding_v5_materialization._EXPECTED_FUNDING_EVENTS",
        1,
    )
    monkeypatch.setattr(
        "market_data_ingestion.funding_v5_materialization.policy_adapter_version",
        lambda _base, _policy: "adapter-v5",
    )

    with pytest.raises(FundingV5MaterializationError) as raised:
        _verify_candidate_without_registry(
            root=tmp_path,
            parent_manifest=parent,
            target_manifest=target,
            policy=cast(RequiredGapPolicy, object()),
        )
    assert raised.value.code == "TARGET_FUNDING_ECONOMICS_OR_TIME_MISMATCH"


def test_candidate_manifest_drift_fails_before_registry_commit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parent, target = _candidate(tmp_path)
    monkeypatch.setattr(
        "market_data_ingestion.funding_v5_materialization._EXPECTED_FUNDING_EVENTS",
        1,
    )
    monkeypatch.setattr(
        "market_data_ingestion.funding_v5_materialization.policy_adapter_version",
        lambda _base, _policy: "adapter-v5",
    )
    drifted = replace(
        target,
        validation=replace(target.validation, rows_checked=target.validation.rows_checked + 1),
    )

    with pytest.raises(FundingV5MaterializationError) as raised:
        _verify_candidate_without_registry(
            root=tmp_path,
            parent_manifest=parent,
            target_manifest=drifted,
            policy=cast(RequiredGapPolicy, object()),
        )
    assert raised.value.code == "TARGET_MANIFEST_CONTRACT_DRIFT"
