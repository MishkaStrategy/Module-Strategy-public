from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.canonical import sha256_file
from market_data_ingestion.contracts import MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RepairStatus
from market_data_ingestion.integrity import verify_registered_dataset
from market_data_ingestion.operations import DatasetOperations
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def test_repair_quarantines_exact_damaged_partition_and_links_audit(
    tmp_path: Path,
) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=storage,
        registry=registry,
    )
    request = MarketDataRequest(
        request_id="repair-quarantine-evidence",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    first = service.execute(request, format_name="csv")
    assert first.dataset is not None
    manifest_path = storage.root / first.dataset.manifest_uri
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    base = next(
        item
        for item in manifest["partitions"]
        if item["data_type"] == "klines" and item["timeframe"] == "5m"
    )
    damaged_path = storage.root / base["storage_uri"]
    damaged_bytes = damaged_path.read_bytes() + b"INTENTIONAL-QUARANTINE-TEST"
    damaged_path.write_bytes(damaged_bytes)

    result = DatasetOperations(
        service=service,
        storage=storage,
        registry=registry,
    ).repair(
        first.dataset.dataset_id,
        data_type=DataType.KLINES,
        format_name="csv",
    )

    assert result.status is RepairStatus.REPAIRED
    assert result.new_dataset_version == 2
    quarantine_files = [item for item in (storage.root / "quarantine").iterdir() if item.is_file()]
    assert len(quarantine_files) == 1
    quarantined = quarantine_files[0]
    assert quarantined.read_bytes() == damaged_bytes

    audit = json.loads((storage.root / result.audit_uri).read_text(encoding="utf-8"))
    evidence = audit["evidence"][0]
    assert storage.root / evidence["quarantine_uri"] == quarantined
    assert evidence["quarantine_sha256"] == sha256_file(quarantined)
    assert evidence["quarantine_byte_size"] == len(damaged_bytes)

    repaired = registry.inspect(first.dataset.dataset_id, 2)
    assert repaired is not None
    assert verify_registered_dataset(storage.root, repaired)["status"] == "ok"
