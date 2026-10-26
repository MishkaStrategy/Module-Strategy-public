from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from market_data_ingestion.adapters.binance import BinanceAdapter
from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
from market_data_ingestion.enums import AdapterMaturity, DataType, MarketType, SourceKind
from market_data_ingestion.errors import AdapterError
from market_data_ingestion.network import HttpClient, HttpResponse


def test_binance_advertises_only_implemented_tested_capabilities() -> None:
    capabilities = BinanceAdapter().get_capabilities()
    assert capabilities.maturity is AdapterMaturity.TESTED
    assert MarketType.SPOT in capabilities.markets
    assert MarketType.USDT_M_PERPETUAL in capabilities.markets
    assert {item.data_type for item in capabilities.data} == {
        DataType.KLINES,
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
        DataType.INSTRUMENT_METADATA,
        DataType.EXCHANGE_METADATA,
    }
    by_type = {item.data_type: item for item in capabilities.data}
    assert by_type[DataType.KLINES].supports_market(MarketType.SPOT)
    for data_type in {
        DataType.FUNDING_RATE,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }:
        assert by_type[data_type].supports_market(MarketType.USDT_M_PERPETUAL)
        assert not by_type[data_type].supports_market(MarketType.SPOT)


def test_binance_resolves_exact_usdtm_perpetual() -> None:
    payload = {
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "baseAsset": "BTC",
                "quoteAsset": "USDT",
                "marginAsset": "USDT",
                "contractType": "PERPETUAL",
                "onboardDate": 1569398400000,
                "deliveryDate": 4133404800000,
                "status": "TRADING",
                "filters": [
                    {"filterType": "PRICE_FILTER", "tickSize": "0.10"},
                    {"filterType": "LOT_SIZE", "stepSize": "0.001"},
                ],
            }
        ]
    }

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        return HttpResponse(200, {}, json.dumps(payload).encode(), url)

    adapter = BinanceAdapter(HttpClient(transport=transport))
    metadata = adapter.resolve_instrument(MarketType.USDT_M_PERPETUAL, "BTC/USDT")
    assert metadata.reference.symbol == "BTCUSDT"
    assert metadata.contract_type == "perpetual"
    assert metadata.tick_size == "0.10"


def test_binance_open_interest_retention_is_explicit() -> None:
    from datetime import timedelta

    import pytest

    from market_data_ingestion.contracts import InstrumentReference, MarketDataRequest
    from market_data_ingestion.enums import DataType
    from market_data_ingestion.errors import AdapterError

    now = datetime(2026, 7, 1, tzinfo=UTC)
    adapter = BinanceAdapter(now=lambda: now)
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    optional = MarketDataRequest(
        request_id="oi-optional",
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(DataType.OPEN_INTEREST_HISTORY,),
        base_timeframe="5m",
        derived_timeframes=(),
        start=now - timedelta(days=60),
        end=now - timedelta(days=1),
    )
    plan = adapter.plan_download(optional, instrument)
    oi_chunks = [item for item in plan.chunks if item.data_type is DataType.OPEN_INTEREST_HISTORY]
    assert oi_chunks
    assert oi_chunks[0].start == now - timedelta(days=30)
    assert any("30-day" in item for item in plan.limitations)

    required = MarketDataRequest(
        request_id="oi-required",
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.OPEN_INTEREST_HISTORY,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=now - timedelta(days=60),
        end=now - timedelta(days=1),
    )
    with pytest.raises(AdapterError, match="30-day retention"):
        adapter.plan_download(required, instrument)


def test_binance_archive_404_falls_back_to_rest_with_truthful_raw_source(tmp_path) -> None:
    from dataclasses import replace
    from datetime import timedelta

    from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
    from market_data_ingestion.enums import DataType, SourceKind
    from market_data_ingestion.storage import LocalStorage

    start = datetime(2026, 1, 1, tzinfo=UTC)
    close = start + timedelta(minutes=5) - timedelta(milliseconds=1)
    page = [
        [
            int(start.timestamp() * 1000),
            "100",
            "102",
            "99",
            "101",
            "10",
            int(close.timestamp() * 1000),
            "1000",
            5,
            "4",
            "400",
        ]
    ]
    calls: list[str] = []

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        calls.append(url)
        if url.endswith(".CHECKSUM"):
            return HttpResponse(404, {}, b"missing", url)
        return HttpResponse(200, {}, json.dumps(page).encode(), url)

    adapter = BinanceAdapter(
        HttpClient(transport=transport),
        now=lambda: datetime(2026, 1, 2, tzinfo=UTC),
    )
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    planned = DownloadChunk(
        chunk_id="archive-fallback",
        data_type=DataType.KLINES,
        start=start,
        end=start + timedelta(minutes=5),
        source=SourceKind.OFFICIAL_ARCHIVE,
        timeframe="5m",
        expected_uri="https://data.binance.vision/archive.zip",
    )
    acquired = adapter.acquire(planned, instrument)
    assert acquired.chunk.source is SourceKind.OFFICIAL_REST
    assert len(acquired.records) == 1
    assert any("/fapi/v1/klines" in call for call in calls)

    storage = LocalStorage(tmp_path / "data")
    descriptor = storage.write_raw(
        run_id="fallback-run",
        instrument=instrument,
        chunk=planned,
        payload=acquired.raw_payload,
        source_uri=acquired.source_uri,
        adapter_version=adapter.adapter_version,
        response_metadata=acquired.response_metadata,
        upstream_checksum=None,
        source_kind=acquired.chunk.source,
    )
    assert descriptor.source is SourceKind.OFFICIAL_REST
    restored_raw = storage.read_raw(instrument, planned)
    assert restored_raw is not None
    payload, evidence = restored_raw
    restored = adapter.restore(
        replace(planned, source=evidence.source, expected_uri=evidence.source_uri),
        instrument,
        payload,
    )
    assert restored.records == acquired.records


def test_binance_premium_index_uses_dedicated_official_endpoint() -> None:
    from datetime import timedelta

    from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
    from market_data_ingestion.enums import SourceKind

    adapter = BinanceAdapter()
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    start = datetime(2026, 1, 1, tzinfo=UTC)
    chunk = DownloadChunk(
        chunk_id="premium-contract",
        data_type=DataType.PREMIUM_INDEX,
        start=start,
        end=start + timedelta(minutes=5),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    path, params, page_size, step = adapter._rest_contract(chunk, instrument)
    assert path == "/fapi/v1/premiumIndexKlines"
    assert params == {"symbol": "BTCUSDT", "interval": "5m"}
    assert page_size == 1000
    assert step == timedelta(minutes=5)


def _zip_payload(entries: dict[str, bytes], *, compression: int | None = None) -> bytes:
    import io
    import zipfile

    output = io.BytesIO()
    method = zipfile.ZIP_DEFLATED if compression is None else compression
    with zipfile.ZipFile(output, "w", compression=method) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return output.getvalue()


def test_binance_archive_rejects_path_traversal_member() -> None:
    import pytest

    from market_data_ingestion.errors import AdapterError

    payload = _zip_payload({"../BTCUSDT-5m.csv": b"unsafe"})
    with pytest.raises(AdapterError) as raised:
        BinanceAdapter._safe_archive_rows(payload)
    assert raised.value.code == "UNSAFE_ARCHIVE"


def test_binance_archive_rejects_multiple_members() -> None:
    import pytest

    from market_data_ingestion.errors import AdapterError

    payload = _zip_payload({"one.csv": b"1", "two.csv": b"2"})
    with pytest.raises(AdapterError) as raised:
        BinanceAdapter._safe_archive_rows(payload)
    assert raised.value.code == "UNSAFE_ARCHIVE"


def test_binance_archive_rejects_truncated_zip() -> None:
    import pytest

    from market_data_ingestion.errors import AdapterError

    with pytest.raises(AdapterError) as raised:
        BinanceAdapter._safe_archive_rows(b"not-a-zip")
    assert raised.value.code == "TRUNCATED_ARCHIVE"


def test_binance_archive_rejects_excessive_compression_ratio() -> None:
    import pytest

    from market_data_ingestion.errors import AdapterError

    payload = _zip_payload({"BTCUSDT-5m.csv": b"0" * 1_000_000})
    with pytest.raises(AdapterError) as raised:
        BinanceAdapter._safe_archive_rows(payload)
    assert raised.value.code == "ARCHIVE_RATIO_LIMIT"


def test_binance_archive_checksum_mismatch_fails_before_unpacking() -> None:
    import hashlib
    from datetime import timedelta

    import pytest

    from market_data_ingestion.contracts import DownloadChunk, InstrumentReference
    from market_data_ingestion.enums import SourceKind
    from market_data_ingestion.errors import AdapterError

    archive = _zip_payload({"BTCUSDT-5m.csv": b"1,2,3\n"})
    wrong_checksum = hashlib.sha256(b"different").hexdigest().encode("ascii")

    def transport(url: str, timeout: float, headers: object) -> HttpResponse:
        del timeout, headers
        body = wrong_checksum if url.endswith(".CHECKSUM") else archive
        return HttpResponse(200, {}, body, url)

    start = datetime(2026, 1, 1, tzinfo=UTC)
    chunk = DownloadChunk(
        chunk_id="checksum-mismatch",
        data_type=DataType.KLINES,
        start=start,
        end=start + timedelta(minutes=5),
        source=SourceKind.OFFICIAL_ARCHIVE,
        timeframe="5m",
        expected_uri="https://data.binance.vision/archive.zip",
    )
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    with pytest.raises(AdapterError) as raised:
        BinanceAdapter(HttpClient(transport=transport))._acquire_archive(chunk, instrument)
    assert raised.value.code == "CHECKSUM_MISMATCH"


def _funding_contract_fixture() -> tuple[DownloadChunk, InstrumentReference, datetime]:
    boundary = datetime(2023, 11, 12, 16, 0, tzinfo=UTC)
    instrument = InstrumentReference(
        exchange_id="binance",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        canonical_id="binance:usdt_m_perpetual:btcusdt",
    )
    chunk = DownloadChunk(
        chunk_id="funding-boundary-normalization",
        data_type=DataType.FUNDING_RATE,
        start=boundary - timedelta(minutes=5),
        end=boundary + timedelta(hours=9),
        source=SourceKind.OFFICIAL_REST,
        expected_uri="https://fapi.binance.com",
    )
    return chunk, instrument, boundary


def test_binance_funding_normalizes_approved_jitter_and_preserves_raw_evidence() -> None:
    chunk, instrument, boundary = _funding_contract_fixture()
    raw_times = (
        int(boundary.timestamp() * 1000),
        int((boundary + timedelta(hours=2)).timestamp() * 1000) + 1,
        int((boundary + timedelta(hours=4)).timestamp() * 1000) + 17,
        int((boundary + timedelta(hours=8)).timestamp() * 1000) + 47,
    )
    page = [
        {
            "symbol": "BTCUSDT",
            "fundingTime": raw_time,
            "fundingRate": "0.0001",
            "markPrice": "37000",
        }
        for raw_time in raw_times
    ]
    raw_payload = BinanceAdapter._encode_pages([json.dumps(page).encode("utf-8")])

    restored = BinanceAdapter().restore(chunk, instrument, raw_payload)

    assert [record.funding_time for record in restored.records] == [
        boundary,
        boundary + timedelta(hours=2),
        boundary + timedelta(hours=4),
        boundary + timedelta(hours=8),
    ]
    decoded = BinanceAdapter._decode_pages(raw_payload)
    assert [row["fundingTime"] for row in decoded[0] if isinstance(row, dict)] == list(raw_times)

    evidence = restored.response_metadata["funding_timestamp_normalization"]
    assert isinstance(evidence, dict)
    assert evidence == {
        "policy": "binance-usdtm-hour-boundary-jitter-v2",
        "canonical_boundary": "utc-hour",
        "approved_positive_jitter_ms_min": 1,
        "approved_positive_jitter_ms_max": 47,
        "raw_timestamp_field": "fundingTime",
        "raw_payload_preserved": True,
        "non_mappable_behavior": "fail_closed",
        "exact_boundary_events": 1,
        "normalized_jitter_events": 3,
        "max_observed_positive_jitter_ms": 47,
    }


def test_binance_funding_restore_is_deterministic_for_same_raw_payload() -> None:
    chunk, instrument, boundary = _funding_contract_fixture()
    page = [
        {
            "symbol": "BTCUSDT",
            "fundingTime": int(boundary.timestamp() * 1000) + 1,
            "fundingRate": "0.0001",
            "markPrice": "37000",
        },
        {
            "symbol": "BTCUSDT",
            "fundingTime": int((boundary + timedelta(hours=8)).timestamp() * 1000) + 47,
            "fundingRate": "-0.0002",
            "markPrice": "37100",
        },
    ]
    raw_payload = BinanceAdapter._encode_pages([json.dumps(page).encode("utf-8")])
    adapter = BinanceAdapter()

    first = adapter.restore(chunk, instrument, raw_payload)
    second = adapter.restore(chunk, instrument, raw_payload)

    assert first.records == second.records
    assert first.response_metadata == second.response_metadata
    assert (
        BinanceAdapter._encode_pages(
            [json.dumps(BinanceAdapter._decode_pages(raw_payload)[0]).encode("utf-8")]
        )
        == raw_payload
    )


def test_binance_funding_rejects_non_mappable_boundary_timestamp() -> None:
    chunk, instrument, boundary = _funding_contract_fixture()
    for offset_ms in (48, -1, 1000):
        page = [
            {
                "symbol": "BTCUSDT",
                "fundingTime": int(boundary.timestamp() * 1000) + offset_ms,
                "fundingRate": "0.0001",
                "markPrice": "37000",
            }
        ]
        raw_payload = BinanceAdapter._encode_pages([json.dumps(page).encode("utf-8")])

        with pytest.raises(AdapterError) as raised:
            BinanceAdapter().restore(chunk, instrument, raw_payload)

        assert raised.value.code == "FUNDING_BOUNDARY_UNMAPPABLE"

    valid_later_boundary = int((boundary + timedelta(hours=1)).timestamp() * 1000)
    page = [
        {
            "symbol": "BTCUSDT",
            "fundingTime": valid_later_boundary,
            "fundingRate": "0.0001",
            "markPrice": "37000",
        }
    ]
    raw_payload = BinanceAdapter._encode_pages([json.dumps(page).encode("utf-8")])
    restored = BinanceAdapter().restore(chunk, instrument, raw_payload)
    assert restored.records[0].funding_time == boundary + timedelta(hours=1)
