from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from market_data_ingestion.adapters.binance import BinanceAdapter
from market_data_ingestion.adapters.fake import FakeExchangeAdapter
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference, MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RunState, SourceKind, ValidationStatus
from market_data_ingestion.network import HttpClient, HttpResponse
from market_data_ingestion.pipeline import IngestionService
from market_data_ingestion.records import FundingRateRecord, OpenInterestRecord, PriceKlineRecord
from market_data_ingestion.registry import SQLiteDatasetRegistry
from market_data_ingestion.storage import LocalStorage


def _request(*, market_type: MarketType = MarketType.USDT_M_PERPETUAL) -> MarketDataRequest:
    return MarketDataRequest(
        request_id=f"multi-{market_type.value}",
        exchange_id="fake",
        market_type=market_type,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES, DataType.FUNDING_RATE, DataType.MARK_PRICE),
        optional_data=(
            DataType.INDEX_PRICE,
            DataType.PREMIUM_INDEX,
            DataType.OPEN_INTEREST_HISTORY,
        ),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )


def test_multitype_pipeline_separates_streams_and_lineage(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=storage,
        registry=registry,
    ).execute(_request(), format_name="csv")

    assert result.final_state is RunState.COMPLETED
    assert result.dataset is not None
    assert set(result.dataset.data_types) == {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
        DataType.INSTRUMENT_METADATA,
        DataType.EXCHANGE_METADATA,
    }
    assert set(result.dataset.timeframes) == {"5m", "15m", "1h"}
    assert result.dataset.validation_status is ValidationStatus.VALID

    manifest = json.loads((storage.root / result.dataset.manifest_uri).read_text())
    pairs = {(item["data_type"], item["timeframe"]) for item in manifest["partitions"]}
    assert ("klines", "5m") in pairs
    assert ("klines", "15m") in pairs
    assert ("klines", "1h") in pairs
    assert ("funding_rate", None) in pairs
    assert ("mark_price", "5m") in pairs
    assert ("index_price", "5m") in pairs
    assert ("premium_index", "5m") in pairs
    assert ("open_interest_history", "5m") in pairs
    assert ("instrument_metadata", None) in pairs
    assert ("exchange_metadata", None) in pairs
    assert {item["data_type"] for item in manifest["data_type_reports"]} == {
        "klines",
        "funding_rate",
        "mark_price",
        "index_price",
        "premium_index",
        "open_interest_history",
        "instrument_metadata",
        "exchange_metadata",
    }
    for partition in manifest["partitions"]:
        assert partition["source_artifact_ids"]
        path = storage.root / partition["storage_uri"]
        assert path.is_file()
        assert f"/{partition['data_type']}/" in f"/{partition['storage_uri']}"


def test_spot_request_rejects_futures_only_required_stream(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    service = IngestionService(
        adapters={"fake": FakeExchangeAdapter()}, storage=storage, registry=registry
    )
    request = MarketDataRequest(
        request_id="spot-funding",
        exchange_id="fake",
        market_type=MarketType.SPOT,
        symbol="BTCUSDT",
        required_data=(DataType.FUNDING_RATE,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 1, tzinfo=UTC),
    )
    import pytest

    from market_data_ingestion.errors import AdapterError

    with pytest.raises(AdapterError, match="requested market"):
        service.execute(request, format_name="csv")


def _instrument() -> InstrumentReference:
    return InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )


def _raw(page: object) -> bytes:
    return BinanceAdapter._encode_pages((json.dumps(page).encode("utf-8"),))


def test_binance_restoration_uses_distinct_auxiliary_record_contracts() -> None:
    adapter = BinanceAdapter(now=lambda: datetime(2026, 1, 2, tzinfo=UTC))
    instrument = _instrument()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    end = start + timedelta(minutes=5)

    price_row = [
        int(start.timestamp() * 1000),
        "100",
        "102",
        "99",
        "101",
        "0",
        int((end - timedelta(milliseconds=1)).timestamp() * 1000),
    ]
    mark_chunk = DownloadChunk(
        chunk_id="mark-1",
        data_type=DataType.MARK_PRICE,
        start=start,
        end=end,
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    mark = adapter.restore(mark_chunk, instrument, _raw([price_row])).records
    assert len(mark) == 1 and isinstance(mark[0], PriceKlineRecord)
    assert mark[0].data_type is DataType.MARK_PRICE

    premium_chunk = DownloadChunk(
        chunk_id="premium-1",
        data_type=DataType.PREMIUM_INDEX,
        start=start,
        end=end,
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    premium = adapter.restore(premium_chunk, instrument, _raw([price_row])).records
    assert len(premium) == 1 and isinstance(premium[0], PriceKlineRecord)
    assert premium[0].data_type is DataType.PREMIUM_INDEX

    funding_chunk = DownloadChunk(
        chunk_id="funding-1",
        data_type=DataType.FUNDING_RATE,
        start=start,
        end=start + timedelta(days=1),
        source=SourceKind.OFFICIAL_REST,
    )
    funding = adapter.restore(
        funding_chunk,
        instrument,
        _raw(
            [
                {
                    "symbol": "BTCUSDT",
                    "fundingTime": int(start.timestamp() * 1000),
                    "fundingRate": "0.0001",
                    "markPrice": "100.5",
                }
            ]
        ),
    ).records
    assert len(funding) == 1 and isinstance(funding[0], FundingRateRecord)

    oi_chunk = DownloadChunk(
        chunk_id="oi-1",
        data_type=DataType.OPEN_INTEREST_HISTORY,
        start=start,
        end=end,
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    open_interest = adapter.restore(
        oi_chunk,
        instrument,
        _raw(
            [
                {
                    "symbol": "BTCUSDT",
                    "timestamp": int(start.timestamp() * 1000),
                    "sumOpenInterest": "123",
                    "sumOpenInterestValue": "456",
                    "CMCCirculatingSupply": "19000000",
                }
            ]
        ),
    ).records
    assert len(open_interest) == 1 and isinstance(open_interest[0], OpenInterestRecord)


def test_metadata_partitions_link_to_raw_resolution_evidence(tmp_path: Path) -> None:
    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter()},
        storage=storage,
        registry=registry,
    ).execute(_request(), format_name="csv")
    assert result.dataset is not None
    manifest = json.loads((storage.root / result.dataset.manifest_uri).read_text())
    artifacts = {item["artifact_id"]: item for item in manifest["source_artifacts"]}
    metadata_partitions = [
        item
        for item in manifest["partitions"]
        if item["data_type"] in {"instrument_metadata", "exchange_metadata"}
    ]
    assert len(metadata_partitions) == 2
    for partition in metadata_partitions:
        assert len(partition["source_artifact_ids"]) == 1
        artifact = artifacts[partition["source_artifact_ids"][0]]
        assert artifact["source"] == "official_rest"
        assert artifact["response_metadata"]["records"] == 1


def test_binance_resolution_preserves_exact_exchange_info_response_bytes() -> None:
    body = json.dumps(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "status": "TRADING",
                    "onboardDate": 1569398400000,
                    "deliveryDate": 4133404800000,
                    "filters": [
                        {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                        {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                    ],
                }
            ]
        },
        separators=(",", ":"),
    ).encode()

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        return HttpResponse(200, {}, body, url)

    adapter = BinanceAdapter(HttpClient(transport=transport))
    resolution = adapter.resolve_instrument_with_evidence(MarketType.USDT_M_PERPETUAL, "BTCUSDT")
    assert resolution.raw_payload == body
    assert resolution.metadata.reference.symbol == "BTCUSDT"
    assert resolution.source is SourceKind.OFFICIAL_REST
    assert resolution.source_uri.endswith("/fapi/v1/exchangeInfo")
