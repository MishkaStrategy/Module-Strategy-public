from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RunState
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.operations import DatasetOperations
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def _base_request(request_id: str = "operations-base") -> MarketDataRequest:
    return MarketDataRequest(
        request_id=request_id,
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )


def _setup(tmp_path: Path) -> tuple[LocalStorage, SQLiteDatasetRegistry, IngestionService]:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    return storage, registry, service


def test_update_creates_new_version_and_reuses_immutable_parent_partitions(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request(), format_name="csv")
    assert first.dataset is not None
    first_manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    first_partition_ids = {
        item["partition_id"]
        for item in first_manifest["partitions"]
        if item["data_type"] not in {"instrument_metadata", "exchange_metadata"}
    }
    first_metadata_ids = {
        item["partition_id"]
        for item in first_manifest["partitions"]
        if item["data_type"] in {"instrument_metadata", "exchange_metadata"}
    }
    assert first.dataset.end == datetime(2026, 1, 1, 1, tzinfo=UTC)

    adapter = FakeExchangeAdapter()
    update_service = IngestionService(
        adapters={"fake": adapter}, storage=storage, registry=registry
    )
    result = DatasetOperations(service=update_service, storage=storage, registry=registry).update(
        first.dataset.dataset_id,
        end=datetime(2026, 1, 1, 2, tzinfo=UTC),
        format_name="csv",
    )

    assert result.final_state is RunState.COMPLETED
    assert result.dataset is not None
    assert result.dataset.version == 2
    assert result.dataset.start == first.dataset.start
    assert result.dataset.end == datetime(2026, 1, 1, 2, tzinfo=UTC)
    second_manifest = json.loads((storage.root / result.dataset.manifest_uri).read_text())
    second_partition_ids = {item["partition_id"] for item in second_manifest["partitions"]}
    assert first_partition_ids < second_partition_ids
    second_metadata_ids = {
        item["partition_id"]
        for item in second_manifest["partitions"]
        if item["data_type"] in {"instrument_metadata", "exchange_metadata"}
    }
    assert second_metadata_ids
    assert first_metadata_ids.isdisjoint(second_metadata_ids)
    assert any(item.startswith("parent:") for item in second_manifest["lineage"])
    assert adapter.acquired_chunks
    assert all(":klines:" in item for item in adapter.acquired_chunks)

    old = registry.inspect(first.dataset.dataset_id, 1)
    assert old is not None and old["dataset_hash"] == first.dataset.dataset_hash


def test_backfill_adds_new_data_type_without_rewriting_old_stream(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request("backfill-base"), format_name="csv")
    assert first.dataset is not None
    first_manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    old_klines = {
        item["partition_id"]
        for item in first_manifest["partitions"]
        if item["data_type"] == "klines"
    }

    result = DatasetOperations(service=service, storage=storage, registry=registry).backfill(
        first.dataset.dataset_id,
        data_type=DataType.FUNDING_RATE,
        format_name="csv",
    )
    assert result.dataset is not None and result.dataset.version == 2
    assert set(result.dataset.data_types) == {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.INSTRUMENT_METADATA,
        DataType.EXCHANGE_METADATA,
    }
    manifest = json.loads((storage.root / result.dataset.manifest_uri).read_text())
    new_klines = {
        item["partition_id"] for item in manifest["partitions"] if item["data_type"] == "klines"
    }
    assert new_klines == old_klines
    assert any(item["data_type"] == "funding_rate" for item in manifest["partitions"])


def test_backfill_refuses_overlap_with_existing_stream(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request("overlap-base"), format_name="csv")
    assert first.dataset is not None
    operations = DatasetOperations(service=service, storage=storage, registry=registry)
    with pytest.raises(StructuredIngestionError, match="overlaps"):
        operations.backfill(
            first.dataset.dataset_id,
            data_type=DataType.KLINES,
            format_name="csv",
        )


def test_repair_replaces_only_damaged_normalized_partition_and_dependents(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request("repair-base"), format_name="csv")
    assert first.dataset is not None
    manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    normalized = next(
        item
        for item in manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "5m"
    )
    damaged_path = storage.root / normalized["storage_uri"]
    damaged_path.write_bytes(damaged_path.read_bytes() + b"corruption")

    result = DatasetOperations(service=service, storage=storage, registry=registry).repair(
        first.dataset.dataset_id,
        data_type=DataType.KLINES,
        format_name="csv",
    )
    from market_data_ingestion.enums import RepairStatus
    from market_data_ingestion.integrity import verify_registered_dataset

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 2
    assert (storage.root / result.audit_uri).is_file()
    repaired = registry.inspect(first.dataset.dataset_id, 2)
    assert repaired is not None
    verification = verify_registered_dataset(storage.root, repaired)
    assert verification["status"] == "ok"
    repaired_manifest = json.loads((storage.root / str(repaired["manifest_uri"])).read_text())
    repaired_base = next(
        item
        for item in repaired_manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "5m"
    )
    assert repaired_base["storage_uri"] != normalized["storage_uri"]
    assert "/v2/" in f"/{repaired_base['storage_uri']}"
    assert any(item.startswith("parent:") for item in repaired_manifest["lineage"])


def test_repair_no_damage_is_idempotent_noop(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request("repair-noop"), format_name="csv")
    assert first.dataset is not None
    result = DatasetOperations(service=service, storage=storage, registry=registry).repair(
        first.dataset.dataset_id, format_name="csv"
    )
    from market_data_ingestion.enums import RepairStatus

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version is None
    assert result.repaired_ranges == ()


def test_repair_rebuilds_damaged_derived_partition_without_network(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request("repair-derived"), format_name="csv")
    assert first.dataset is not None
    first_manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    damaged = next(
        item
        for item in first_manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "15m"
    )
    original_normalized_ids = {
        item["partition_id"]
        for item in first_manifest["partitions"]
        if item["storage_uri"].startswith("normalized/")
    }
    path = storage.root / damaged["storage_uri"]
    path.write_bytes(path.read_bytes() + b"derived-corruption")

    adapter = FakeExchangeAdapter()
    repair_service = IngestionService(
        adapters={"fake": adapter}, storage=storage, registry=registry
    )
    result = DatasetOperations(service=repair_service, storage=storage, registry=registry).repair(
        first.dataset.dataset_id, data_type=DataType.KLINES, format_name="csv"
    )

    from market_data_ingestion.enums import RepairStatus
    from market_data_ingestion.integrity import verify_registered_dataset

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 2
    assert adapter.acquired_chunks == []
    repaired = registry.inspect(first.dataset.dataset_id, 2)
    assert repaired is not None
    assert verify_registered_dataset(storage.root, repaired)["status"] == "ok"
    second_manifest = json.loads((storage.root / str(repaired["manifest_uri"])).read_text())
    assert {
        item["partition_id"]
        for item in second_manifest["partitions"]
        if item["storage_uri"].startswith("normalized/")
    } == original_normalized_ids
    rebuilt = next(
        item
        for item in second_manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "15m"
    )
    assert rebuilt["partition_id"] == damaged["partition_id"]
    assert rebuilt["storage_uri"] != damaged["storage_uri"]
    assert "/v2/" in f"/{rebuilt['storage_uri']}"
    audit = json.loads((storage.root / result.audit_uri).read_text())
    assert audit["mode"] == "derived_local_rebuild"


def test_repair_restores_metadata_snapshot_from_raw_without_network(tmp_path: Path) -> None:
    storage, registry, service = _setup(tmp_path)
    first = service.execute(_base_request("repair-metadata"), format_name="csv")
    assert first.dataset is not None
    first_manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    damaged = next(
        item for item in first_manifest["partitions"] if item["data_type"] == "instrument_metadata"
    )
    path = storage.root / damaged["storage_uri"]
    path.write_bytes(path.read_bytes() + b"metadata-corruption")

    adapter = FakeExchangeAdapter()
    repair_service = IngestionService(
        adapters={"fake": adapter}, storage=storage, registry=registry
    )
    result = DatasetOperations(service=repair_service, storage=storage, registry=registry).repair(
        first.dataset.dataset_id,
        data_type=DataType.INSTRUMENT_METADATA,
        format_name="csv",
    )

    from market_data_ingestion.enums import RepairStatus
    from market_data_ingestion.integrity import verify_registered_dataset

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 2
    assert adapter.acquired_chunks == []
    repaired = registry.inspect(first.dataset.dataset_id, 2)
    assert repaired is not None
    assert verify_registered_dataset(storage.root, repaired)["status"] == "ok"
    assert repaired["dataset_hash"] == first.dataset.dataset_hash
    second_manifest = json.loads((storage.root / str(repaired["manifest_uri"])).read_text())
    replacement = next(
        item for item in second_manifest["partitions"] if item["data_type"] == "instrument_metadata"
    )
    assert replacement["partition_id"] == damaged["partition_id"]
    assert replacement["storage_uri"] != damaged["storage_uri"]
    audit = json.loads((storage.root / result.audit_uri).read_text())
    assert audit["mode"] == "metadata_from_raw"
    assert audit["raw_artifact_id"] in damaged["source_artifact_ids"]


def test_repair_fetches_only_known_gap_and_merges_healthy_partition(tmp_path: Path) -> None:
    from market_data_ingestion.adapters.base import AcquiredData
    from market_data_ingestion.enums import RepairStatus, ValidationStatus
    from market_data_ingestion.integrity import verify_registered_dataset

    class GappedFakeAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            acquired = super().acquire(chunk, instrument)
            if chunk.data_type is DataType.KLINES and len(acquired.records) > 6:
                records = tuple(item for index, item in enumerate(acquired.records) if index != 5)
                return AcquiredData(
                    chunk=acquired.chunk,
                    source_uri=acquired.source_uri,
                    raw_payload=self._kline_csv(records),
                    records=records,
                    response_metadata={"records": len(records), "injected_gap": True},
                )
            return acquired

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    gapped = GappedFakeAdapter()
    first = IngestionService(adapters={"fake": gapped}, storage=storage, registry=registry).execute(
        _base_request("repair-known-gap"),
        format_name="csv",
        reject_unresolved_required_gaps=False,
    )
    assert first.dataset is not None
    assert first.dataset.validation_status is ValidationStatus.VALID_WITH_WARNINGS
    first_manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    assert len(first_manifest["validation"]["gaps"]) == 1
    old_base = next(
        item
        for item in first_manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "5m"
    )

    healthy = FakeExchangeAdapter()
    result = DatasetOperations(
        service=IngestionService(adapters={"fake": healthy}, storage=storage, registry=registry),
        storage=storage,
        registry=registry,
    ).repair(first.dataset.dataset_id, data_type=DataType.KLINES, format_name="csv")

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 2
    assert len(result.repaired_ranges) == 1
    assert len(healthy.acquired_chunks) == 1
    repaired = registry.inspect(first.dataset.dataset_id, 2)
    assert repaired is not None
    assert repaired["validation_status"] == "VALID"
    assert repaired["unresolved_issues"] == []
    assert verify_registered_dataset(storage.root, repaired)["status"] == "ok"
    second_manifest = json.loads((storage.root / str(repaired["manifest_uri"])).read_text())
    assert second_manifest["validation"]["gaps"] == []
    new_base = next(
        item
        for item in second_manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "5m"
    )
    assert new_base["row_count"] == old_base["row_count"] + 1
    assert new_base["storage_uri"] != old_base["storage_uri"]
    audit = json.loads((storage.root / result.audit_uri).read_text())
    assert audit["mode"] == "gap_range_repair"
    assert len(audit["raw_artifact_ids"]) == 1


def test_repair_multiple_damaged_streams_creates_staged_immutable_versions(
    tmp_path: Path,
) -> None:
    from market_data_ingestion.enums import RepairStatus
    from market_data_ingestion.integrity import verify_registered_dataset

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    request = MarketDataRequest(
        request_id="multi-stream-repair",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES, DataType.FUNDING_RATE),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    first = service.execute(request, format_name="csv")
    assert first.dataset is not None
    manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    damaged = [
        next(
            item
            for item in manifest["partitions"]
            if item["data_type"] == "klines" and item["timeframe"] == "5m"
        ),
        next(item for item in manifest["partitions"] if item["data_type"] == "funding_rate"),
    ]
    for item in damaged:
        path = storage.root / item["storage_uri"]
        path.write_bytes(path.read_bytes() + b"multi-stream-corruption")

    result = DatasetOperations(service=service, storage=storage, registry=registry).repair(
        first.dataset.dataset_id, format_name="csv"
    )

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 3
    assert registry.inspect(first.dataset.dataset_id, 2) is not None
    final = registry.inspect(first.dataset.dataset_id, 3)
    assert final is not None
    assert verify_registered_dataset(storage.root, final)["status"] == "ok"
    audit = json.loads((storage.root / result.audit_uri).read_text())
    assert audit["mode"] == "multi_stream_staged_repair"
    assert audit["damaged_data_types"] == ["funding_rate", "klines"]
    assert len(audit["child_audits"]) == 2


@pytest.mark.parametrize(
    "data_type",
    [
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    ],
)
def test_repair_fetches_only_known_auxiliary_gap(tmp_path: Path, data_type: DataType) -> None:
    from market_data_ingestion.adapters.base import AcquiredData
    from market_data_ingestion.enums import RepairStatus, ValidationStatus
    from market_data_ingestion.integrity import verify_registered_dataset

    class GappedAuxiliaryAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            acquired = super().acquire(chunk, instrument)
            if chunk.data_type is data_type and len(acquired.records) > 6:
                records = tuple(item for index, item in enumerate(acquired.records) if index != 5)
                return AcquiredData(
                    chunk=acquired.chunk,
                    source_uri=acquired.source_uri,
                    raw_payload=self._json_payload(records),
                    records=records,
                    response_metadata={
                        "records": len(records),
                        "injected_gap": True,
                    },
                )
            return acquired

    storage = LocalStorage(tmp_path / data_type.value)
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = MarketDataRequest(
        request_id=f"repair-{data_type.value}-gap",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(data_type,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    first = IngestionService(
        adapters={"fake": GappedAuxiliaryAdapter()},
        storage=storage,
        registry=registry,
    ).execute(
        request,
        format_name="csv",
        reject_unresolved_required_gaps=False,
    )
    assert first.dataset is not None
    assert first.dataset.validation_status is ValidationStatus.VALID_WITH_WARNINGS
    first_manifest = json.loads((storage.root / first.dataset.manifest_uri).read_text())
    stream_report = next(
        item for item in first_manifest["data_type_reports"] if item["data_type"] == data_type.value
    )
    assert len(stream_report["report"]["gaps"]) == 1
    old_partition = next(
        item
        for item in first_manifest["partitions"]
        if item["data_type"] == data_type.value and item["timeframe"] == "5m"
    )

    healthy = FakeExchangeAdapter()
    result = DatasetOperations(
        service=IngestionService(adapters={"fake": healthy}, storage=storage, registry=registry),
        storage=storage,
        registry=registry,
    ).repair(first.dataset.dataset_id, data_type=data_type, format_name="csv")

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 2
    assert len(result.repaired_ranges) == 1
    assert len(healthy.acquired_chunks) == 1
    assert f":{data_type.value}:" in healthy.acquired_chunks[0]
    repaired = registry.inspect(first.dataset.dataset_id, 2)
    assert repaired is not None
    assert repaired["validation_status"] == "VALID"
    assert verify_registered_dataset(storage.root, repaired)["status"] == "ok"
    second_manifest = json.loads((storage.root / str(repaired["manifest_uri"])).read_text())
    new_partition = next(
        item
        for item in second_manifest["partitions"]
        if item["data_type"] == data_type.value and item["timeframe"] == "5m"
    )
    assert new_partition["row_count"] == old_partition["row_count"] + 1
    assert new_partition["storage_uri"] != old_partition["storage_uri"]
    assert second_manifest["validation"]["gaps"] == []
    audit = json.loads((storage.root / result.audit_uri).read_text())
    assert audit["data_type"] == data_type.value
    assert audit["mode"] == "gap_range_repair"


def test_auxiliary_gap_repair_rejects_wrong_stream_record(tmp_path: Path) -> None:
    from dataclasses import replace

    from market_data_ingestion.adapters.base import AcquiredData
    from market_data_ingestion.enums import RepairStatus

    target = DataType.MARK_PRICE

    class GappedMarkAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            acquired = super().acquire(chunk, instrument)
            if chunk.data_type is target and len(acquired.records) > 6:
                records = tuple(item for index, item in enumerate(acquired.records) if index != 5)
                return AcquiredData(
                    chunk=acquired.chunk,
                    source_uri=acquired.source_uri,
                    raw_payload=self._json_payload(records),
                    records=records,
                    response_metadata={"records": len(records)},
                )
            return acquired

    class WrongStreamAdapter(FakeExchangeAdapter):
        def acquire(self, chunk, instrument):  # type: ignore[no-untyped-def]
            acquired = super().acquire(chunk, instrument)
            wrong = tuple(
                replace(item, price_type=DataType.INDEX_PRICE) for item in acquired.records
            )
            return AcquiredData(
                chunk=acquired.chunk,
                source_uri=acquired.source_uri,
                raw_payload=self._json_payload(wrong),
                records=wrong,
                response_metadata={"records": len(wrong)},
            )

    storage = LocalStorage(tmp_path / "wrong-stream")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    request = MarketDataRequest(
        request_id="wrong-stream-base",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(target,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    first = IngestionService(
        adapters={"fake": GappedMarkAdapter()},
        storage=storage,
        registry=registry,
    ).execute(
        request,
        format_name="csv",
        reject_unresolved_required_gaps=False,
    )
    assert first.dataset is not None

    result = DatasetOperations(
        service=IngestionService(
            adapters={"fake": WrongStreamAdapter()},
            storage=storage,
            registry=registry,
        ),
        storage=storage,
        registry=registry,
    ).repair(first.dataset.dataset_id, data_type=target, format_name="csv")

    assert result.status is RepairStatus.SOURCE_CONFLICT
    assert result.new_dataset_version is None
    assert registry.inspect(first.dataset.dataset_id, 2) is None
    audit = json.loads((storage.root / result.audit_uri).read_text())
    assert audit["status"] == "source_contract_violation"
    assert audit["error_type"] == "ValueError"
