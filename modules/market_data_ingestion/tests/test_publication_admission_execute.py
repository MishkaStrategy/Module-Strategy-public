from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_ingestion import pipeline as pipeline_module
from market_data_ingestion import publication_admission
from market_data_ingestion.adapters.base import AcquiredData, ExchangeAdapter
from market_data_ingestion.contracts import (
    DataTypeCapability,
    DownloadChunk,
    DownloadPlan,
    ExchangeCapabilities,
    ExchangeReference,
    InstrumentMetadata,
    InstrumentReference,
    MarketDataRequest,
)
from market_data_ingestion.enums import AdapterMaturity, DataType, MarketType, SourceKind
from market_data_ingestion.errors import StructuredIngestionError
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.publication_admission import PartitionPublicationPlan
from market_data_ingestion.records import KlineRecord
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


class _AdmissionAdapter(ExchangeAdapter):
    exchange_id = "fixture"
    adapter_version = "1.0.0"

    def __init__(self) -> None:
        self.instrument = InstrumentReference(
            exchange_id=self.exchange_id,
            market_type=MarketType.USDT_M_PERPETUAL,
            symbol="BTCUSDT",
            canonical_id="fixture:usdtm:btcusdt",
        )
        start = datetime(2026, 1, 1, tzinfo=UTC)
        self.records = tuple(
            KlineRecord(
                open_time=start + timedelta(minutes=5 * index),
                close_time=start + timedelta(minutes=5 * (index + 1)),
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
                exchange_id=self.exchange_id,
                market_type=MarketType.USDT_M_PERPETUAL.value,
                source_kind=SourceKind.OFFICIAL_REST.value,
            )
            for index in range(3)
        )

    def get_capabilities(self) -> ExchangeCapabilities:
        return ExchangeCapabilities(
            exchange=ExchangeReference(self.exchange_id, self.adapter_version),
            markets=(MarketType.USDT_M_PERPETUAL,),
            data=(
                DataTypeCapability(
                    data_type=DataType.KLINES,
                    timeframes=("5m",),
                    sources=(SourceKind.OFFICIAL_REST,),
                    markets=(MarketType.USDT_M_PERPETUAL,),
                ),
            ),
            maximum_page_size=1000,
            timestamp_unit="ms",
            maturity=AdapterMaturity.TESTED,
            rate_limit_policy={"mode": "fixture"},
        )

    def resolve_instrument(self, market_type: object, symbol: str) -> InstrumentMetadata:
        if market_type is not MarketType.USDT_M_PERPETUAL or symbol.upper() != "BTCUSDT":
            raise AssertionError("unexpected fixture instrument request")
        return InstrumentMetadata(
            reference=self.instrument,
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_type="PERPETUAL",
            listing_date=datetime(2025, 1, 1, tzinfo=UTC),
            delisting_date=None,
            tick_size="0.1",
            quantity_step="0.001",
            contract_multiplier="1",
            trading_status="TRADING",
        )

    def plan_download(
        self,
        request: MarketDataRequest,
        instrument: InstrumentReference,
    ) -> DownloadPlan:
        assert instrument == self.instrument
        assert request.start is not None
        return DownloadPlan(
            request_hash=request.canonical_hash,
            instrument=instrument,
            chunks=(
                DownloadChunk(
                    chunk_id="fixture:klines:0",
                    data_type=DataType.KLINES,
                    timeframe="5m",
                    start=request.start,
                    end=request.end,
                    source=SourceKind.OFFICIAL_REST,
                    expected_uri="https://example.invalid/fixture-klines",
                ),
            ),
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
        )

    def acquire(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
    ) -> AcquiredData:
        assert instrument == self.instrument
        return AcquiredData(
            chunk=chunk,
            source_uri="https://example.invalid/fixture-klines",
            raw_payload=b"fixture-klines\n",
            records=self.records,
            response_metadata={"records": len(self.records)},
        )

    def restore(
        self,
        chunk: DownloadChunk,
        instrument: InstrumentReference,
        raw_payload: bytes,
    ) -> AcquiredData:
        assert raw_payload == b"fixture-klines\n"
        return self.acquire(chunk, instrument)


def _request() -> MarketDataRequest:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return MarketDataRequest(
        request_id="request-595-owner",
        exchange_id="fixture",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=start,
        end=start + timedelta(minutes=15),
    )


def _request_descriptor_json(path: Path, request_id: str) -> str | None:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            "SELECT descriptor_json FROM ingestion_requests WHERE request_id=?",
            (request_id,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    value = row[0]
    assert value is None or isinstance(value, str)
    return value


def test_owner_rejects_before_version_publication_then_retries_exactly_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "storage")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    adapter = _AdmissionAdapter()
    service = IngestionService(
        adapters={adapter.exchange_id: adapter}, storage=storage, registry=registry
    )
    request = _request()
    observed_manifest_bytes: list[int] = []
    real_require_capacity = publication_admission.require_publication_capacity

    def capture_capacity(
        *,
        storage: LocalStorage,
        registry_path: Path,
        plans: Iterable[PartitionPublicationPlan],
        manifest_byte_size: int,
        format_name: str,
    ) -> None:
        observed_manifest_bytes.append(manifest_byte_size)
        real_require_capacity(
            storage=storage,
            registry_path=registry_path,
            plans=plans,
            manifest_byte_size=manifest_byte_size,
            format_name=format_name,
        )

    monkeypatch.setattr(pipeline_module, "require_publication_capacity", capture_capacity)
    monkeypatch.setattr(publication_admission, "_free_bytes", lambda _path: 0)
    with pytest.raises(StructuredIngestionError) as caught:
        service.execute(request, format_name="csv")

    error = caught.value
    assert error.code == "PUBLICATION_CAPACITY_INSUFFICIENT"
    assert error.retryable is True
    assert error.stage == "storage"
    assert error.details["shared_filesystem"] is True
    rejected_manifest_bytes = error.details["manifest_bytes"]
    assert isinstance(rejected_manifest_bytes, int)
    assert rejected_manifest_bytes > 0
    assert observed_manifest_bytes == [rejected_manifest_bytes]
    assert _request_descriptor_json(registry.path, request.request_id) is None
    assert registry.health()["datasets"] == 0
    assert SQLiteDatasetRegistry(registry.path).health()["datasets"] == 0
    assert tuple((storage.root / "raw").rglob("*"))
    assert tuple((storage.root / "checkpoints").glob("*.json"))
    assert tuple((storage.root / "runs").glob("*.json"))
    assert not tuple((storage.root / "normalized").rglob("v1/*"))
    assert not tuple((storage.root / "derived").rglob("v1/*"))
    assert not tuple((storage.root / "manifests").rglob("v1.json"))
    assert not tuple(storage.root.rglob(".*.tmp"))

    monkeypatch.setattr(publication_admission, "_free_bytes", lambda _path: 1 << 40)
    recovered = service.execute(request, format_name="csv")
    assert recovered.dataset is not None
    assert recovered.dataset.version == 1
    assert registry.health()["datasets"] == 1
    assert SQLiteDatasetRegistry(registry.path).health()["datasets"] == 1
    assert _request_descriptor_json(registry.path, request.request_id) is not None
    assert len(observed_manifest_bytes) == 2
    manifest_path = storage.root / recovered.dataset.manifest_uri
    assert manifest_path.is_file()
    assert manifest_path.stat().st_size == observed_manifest_bytes[-1]

    repeated = service.execute(request, format_name="csv")
    assert repeated.dataset == recovered.dataset
    assert registry.health()["datasets"] == 1
    assert len(observed_manifest_bytes) == 2
    assert not tuple((storage.root / "manifests" / recovered.dataset.dataset_id).glob("v2.json"))
