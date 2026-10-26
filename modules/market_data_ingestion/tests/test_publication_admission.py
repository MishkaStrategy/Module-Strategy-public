from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_ingestion import publication_admission
from market_data_ingestion.contracts import InstrumentReference
from market_data_ingestion.enums import DataType, MarketType
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.publication_admission import (
    MARKET_DATA_REGISTRY_RESERVE_BYTES,
    PartitionPublicationPlan,
    plan_partition,
    require_publication_capacity,
)
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def _instrument() -> InstrumentReference:
    return InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdtm:btcusdt",
    )


def _records() -> tuple[KlineRecord, ...]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    output: list[KlineRecord] = []
    for index in range(3):
        open_time = start + timedelta(minutes=5 * index)
        output.append(
            KlineRecord(
                open_time=open_time,
                close_time=open_time + timedelta(minutes=5),
                open="100",
                high="102",
                low="99",
                close="101",
                volume="10",
                quote_volume="1010",
                trade_count=2,
                taker_buy_base_volume="5",
                taker_buy_quote_volume="505",
                symbol="BTCUSDT",
                timeframe="5m",
                exchange_id="binance",
                market_type=MarketType.USDT_M_PERPETUAL.value,
                source_kind="official_archive",
            )
        )
    return tuple(output)


def _plan(
    storage: LocalStorage, records: tuple[KlineRecord, ...], format_name: str
) -> PartitionPublicationPlan:
    return plan_partition(
        storage=storage,
        dataset_id="binance-usdtm-btcusdt-5m",
        version=1,
        layer="normalized",
        instrument=_instrument(),
        data_type=DataType.KLINES,
        timeframe="5m",
        records=records,
        source_artifact_ids=("raw:test",),
        format_name=format_name,
        compression="zstd",
    )


def _round_up(value: int, unit: int) -> int:
    return ((value + unit - 1) // unit) * unit


@pytest.mark.parametrize("format_name", ["csv", "parquet"])
def test_partition_plan_matches_published_bytes_and_descriptor(
    tmp_path: Path,
    format_name: str,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    records = _records()
    plan = _plan(storage, records, format_name)

    assert isinstance(plan, PartitionPublicationPlan)
    assert plan.byte_size > 0
    target = storage.root / plan.descriptor.storage_uri
    assert not target.exists()
    assert not tuple(target.parent.glob(f".{target.name}.*.tmp"))

    published = storage.write_partition(
        dataset_id="binance-usdtm-btcusdt-5m",
        version=1,
        layer="normalized",
        instrument=_instrument(),
        data_type=DataType.KLINES,
        timeframe="5m",
        records=records,
        source_artifact_ids=("raw:test",),
        format_name=format_name,
        compression="zstd",
    )

    assert published == plan.descriptor
    assert target.stat().st_size == plan.byte_size


@pytest.mark.parametrize("format_name", ["csv", "parquet"])
def test_publication_admission_rejects_before_first_version_write_and_preserves_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    format_name: str,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(tmp_path / "registry.sqlite3")
    records = _records()
    plan = _plan(storage, records, format_name)
    manifest_byte_size = 321
    request_hash = "a" * 64
    assert registry.claim_request("request-595", request_hash) is None

    required = plan.byte_size + manifest_byte_size + MARKET_DATA_REGISTRY_RESERVE_BYTES
    monkeypatch.setattr(publication_admission, "_same_filesystem", lambda _left, _right: True)
    monkeypatch.setattr(publication_admission, "_allocation_unit", lambda _path: 1)
    monkeypatch.setattr(publication_admission, "_free_bytes", lambda _path: required - 1)
    with pytest.raises(StructuredIngestionError) as caught:
        require_publication_capacity(
            storage=storage,
            registry_path=registry.path,
            plans=(plan,),
            manifest_byte_size=manifest_byte_size,
            format_name=format_name,
        )

    error = caught.value
    assert error.code == "PUBLICATION_CAPACITY_INSUFFICIENT"
    assert error.retryable is True
    assert error.stage == "storage"
    assert error.details["available_bytes"] == required - 1
    assert error.details["required_bytes"] == required
    assert error.details["partition_bytes"] == plan.byte_size
    assert error.details["partition_allocated_bytes"] == plan.byte_size
    assert error.details["manifest_bytes"] == manifest_byte_size
    assert error.details["manifest_allocated_bytes"] == manifest_byte_size
    assert error.details["registry_reserve_bytes"] == MARKET_DATA_REGISTRY_RESERVE_BYTES
    assert error.details["registry_allocated_bytes"] == MARKET_DATA_REGISTRY_RESERVE_BYTES
    assert error.details["allocation_unit_bytes"] == 1
    assert error.details["partition_count"] == 1
    assert error.details["format"] == format_name
    assert error.details["shared_filesystem"] is True
    assert registry.claim_request("request-595", request_hash) is None
    assert not tuple((storage.root / "normalized").rglob("v1/*"))
    assert not tuple((storage.root / "derived").rglob("v1/*"))

    monkeypatch.setattr(publication_admission, "_free_bytes", lambda _path: required)
    require_publication_capacity(
        storage=storage,
        registry_path=registry.path,
        plans=(plan,),
        manifest_byte_size=manifest_byte_size,
        format_name=format_name,
    )
    published = storage.write_partition(
        dataset_id="binance-usdtm-btcusdt-5m",
        version=1,
        layer="normalized",
        instrument=_instrument(),
        data_type=DataType.KLINES,
        timeframe="5m",
        records=records,
        source_artifact_ids=("raw:test",),
        format_name=format_name,
        compression="zstd",
    )
    assert published == plan.descriptor


def test_publication_admission_rounds_future_files_to_allocation_units(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    plan = _plan(storage, _records(), "csv")
    manifest_byte_size = 321
    allocation_unit = 4096
    partition_allocated = _round_up(plan.byte_size, allocation_unit)
    manifest_allocated = _round_up(manifest_byte_size, allocation_unit)
    registry_allocated = _round_up(MARKET_DATA_REGISTRY_RESERVE_BYTES, allocation_unit)
    required = partition_allocated + manifest_allocated + registry_allocated

    monkeypatch.setattr(publication_admission, "_same_filesystem", lambda _left, _right: True)
    monkeypatch.setattr(
        publication_admission,
        "_allocation_unit",
        lambda _path: allocation_unit,
    )
    monkeypatch.setattr(publication_admission, "_free_bytes", lambda _path: required - 1)

    with pytest.raises(StructuredIngestionError) as caught:
        require_publication_capacity(
            storage=storage,
            registry_path=registry.path,
            plans=(plan,),
            manifest_byte_size=manifest_byte_size,
            format_name="csv",
        )

    error = caught.value
    assert error.details["required_bytes"] == required
    assert error.details["partition_bytes"] == plan.byte_size
    assert error.details["partition_allocated_bytes"] == partition_allocated
    assert error.details["manifest_bytes"] == manifest_byte_size
    assert error.details["manifest_allocated_bytes"] == manifest_allocated
    assert error.details["registry_allocated_bytes"] == registry_allocated
    assert error.details["allocation_unit_bytes"] == allocation_unit


def test_publication_admission_checks_registry_mount_separately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    registry_path = tmp_path / "registry" / "datasets.sqlite3"
    registry_path.parent.mkdir(parents=True)
    plan = _plan(storage, _records(), "csv")
    manifest_byte_size = 321
    storage_required = plan.byte_size + manifest_byte_size

    monkeypatch.setattr(publication_admission, "_same_filesystem", lambda _left, _right: False)
    monkeypatch.setattr(publication_admission, "_allocation_unit", lambda _path: 1)

    def free_bytes(path: Path) -> int:
        return (
            storage_required
            if path.resolve() == storage.root
            else MARKET_DATA_REGISTRY_RESERVE_BYTES - 1
        )

    monkeypatch.setattr(publication_admission, "_free_bytes", free_bytes)
    with pytest.raises(StructuredIngestionError) as caught:
        require_publication_capacity(
            storage=storage,
            registry_path=registry_path,
            plans=(plan,),
            manifest_byte_size=manifest_byte_size,
            format_name="csv",
        )

    error = caught.value
    assert error.code == "PUBLICATION_CAPACITY_INSUFFICIENT"
    assert error.details["shared_filesystem"] is False
    assert error.details["storage_allocation_unit_bytes"] == 1
    assert error.details["storage_available_bytes"] == storage_required
    assert error.details["storage_required_bytes"] == storage_required
    assert error.details["registry_allocation_unit_bytes"] == 1
    assert error.details["registry_available_bytes"] == MARKET_DATA_REGISTRY_RESERVE_BYTES - 1
    assert error.details["registry_required_bytes"] == MARKET_DATA_REGISTRY_RESERVE_BYTES
    assert error.details["registry_reserve_bytes"] == MARKET_DATA_REGISTRY_RESERVE_BYTES
    assert str(tmp_path) not in str(error.details)
