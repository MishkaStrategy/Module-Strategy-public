from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

from market_data_ingestion.aggregation import aggregate_klines
from market_data_ingestion.contracts import (
    DatasetManifest,
    DataValidationReport,
    ExchangeReference,
    NormalizedPartitionDescriptor,
    RawArtifactDescriptor,
)
from market_data_ingestion.enums import (
    DataType,
    MarketType,
    SourceKind,
    ValidationStatus,
)
from market_data_ingestion.hashing import dataset_hash, merkle_root


def test_aggregation_builds_only_complete_groups(five_minute_records) -> None:
    result = aggregate_klines(five_minute_records, target_timeframe="15m")
    assert len(result) == 4
    assert result[0].open == five_minute_records[0].open
    assert result[0].close == five_minute_records[2].close
    assert result[0].volume == "30"
    incomplete = aggregate_klines(five_minute_records[:-1], target_timeframe="15m")
    assert len(incomplete) == 3


def test_merkle_root_is_order_sensitive_at_caller_boundary() -> None:
    a = "00" * 32
    b = "11" * 32
    assert merkle_root((a, b)) != merkle_root((b, a))


def test_dataset_hash_ignores_creation_time_but_tracks_partition_hash(instrument) -> None:
    report = DataValidationReport(
        status=ValidationStatus.VALID,
        issues=(),
        gaps=(),
        duplicates=(),
        rows_checked=1,
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    partition = NormalizedPartitionDescriptor(
        partition_id="part:000",
        data_type=DataType.KLINES,
        timeframe="5m",
        storage_uri="normalized/a.parquet",
        checksum_sha256="00" * 32,
        row_count=1,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        schema_version=1,
        source_artifact_ids=("raw:000",),
    )
    raw = RawArtifactDescriptor(
        artifact_id="raw:000",
        source=SourceKind.OFFICIAL_REST,
        source_uri="https://example.invalid/klines",
        retrieved_at=datetime(2026, 1, 1, tzinfo=UTC),
        checksum_sha256="22" * 32,
        byte_size=1,
        record_count=1,
        start=partition.start,
        end=partition.end,
        adapter_version="1.0.0",
    )
    manifest = DatasetManifest(
        dataset_id="fake_usdt-btc_5m",
        version=1,
        exchange=ExchangeReference("fake", "1.0.0"),
        market_type=MarketType.USDT_M_PERPETUAL,
        instrument=instrument,
        data_types=(DataType.KLINES,),
        timeframes=("5m",),
        start=partition.start,
        end=partition.end,
        partitions=(partition,),
        source_artifacts=(raw,),
        validation=report,
        normalization_version="1.0.0",
        validation_policy_version="1.0.0",
        schema_version=1,
        lineage=("request:x",),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
    )
    assert dataset_hash(manifest) == dataset_hash(
        replace(manifest, created_at=datetime(2026, 2, 1, tzinfo=UTC))
    )
    assert dataset_hash(manifest) == dataset_hash(
        replace(manifest, dataset_id="renamed_dataset", version=99)
    )
    changed = replace(partition, checksum_sha256="11" * 32)
    assert dataset_hash(manifest) != dataset_hash(replace(manifest, partitions=(changed,)))


def test_streaming_aggregation_matches_reference(five_minute_records) -> None:
    from market_data_ingestion.aggregation import (
        aggregate_klines,
        aggregate_klines_stream,
    )

    expected = aggregate_klines(five_minute_records, target_timeframe="15m")
    actual = tuple(aggregate_klines_stream(five_minute_records, target_timeframe="15m"))

    assert actual == expected
