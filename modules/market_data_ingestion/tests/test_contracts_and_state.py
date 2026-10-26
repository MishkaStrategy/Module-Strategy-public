from __future__ import annotations

from datetime import UTC, datetime

import pytest

from market_data_ingestion.adapters.base import AcquiredData
from market_data_ingestion.contracts import DownloadChunk, MarketDataRequest
from market_data_ingestion.enums import DataType, MarketType, RunState, SourceKind
from market_data_ingestion.errors import ContractError
from market_data_ingestion.state_machine import IngestionStateMachine
from market_data_ingestion.timeframes import floor_timeframe, is_timeframe_aligned


def request(request_id: str = "request-001") -> MarketDataRequest:
    return MarketDataRequest(
        request_id=request_id,
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTC/USDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m", "1h"),
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 2, tzinfo=UTC),
    )


def test_request_hash_ignores_request_id() -> None:
    assert request("request-001").canonical_hash == request("request-002").canonical_hash


def test_derived_timeframe_must_be_multiple() -> None:
    with pytest.raises(ValueError, match="integer multiple"):
        MarketDataRequest(
            request_id="request-001",
            exchange_id="fake",
            market_type=MarketType.SPOT,
            symbol="BTCUSDT",
            required_data=(DataType.KLINES,),
            optional_data=(),
            base_timeframe="5m",
            derived_timeframes=("7m",),
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 2, tzinfo=UTC),
        )


def test_state_machine_rejects_invalid_transition() -> None:
    machine = IngestionStateMachine()
    with pytest.raises(ContractError):
        machine.transition(RunState.DOWNLOADING)
    machine.transition(RunState.VALIDATING_REQUEST)
    assert machine.state is RunState.VALIDATING_REQUEST


def test_utc_alignment() -> None:
    value = datetime(2026, 1, 1, 0, 7, tzinfo=UTC)
    assert floor_timeframe(value, "5m") == datetime(2026, 1, 1, 0, 5, tzinfo=UTC)
    assert is_timeframe_aligned(datetime(2026, 1, 1, 0, 5, tzinfo=UTC), "5m")
    assert not is_timeframe_aligned(value, "5m")


@pytest.mark.parametrize(
    ("field_name", "field_value", "error_type"),
    [
        ("source_uri", " ", ValueError),
        ("source_uri", "https://example.invalid/data?api_key=secret", ValueError),
        ("source_uri", "https://user:secret@example.invalid/data", ValueError),
        ("source_uri", "https://example.invalid/data#token", ValueError),
        ("source_uri", "relative/path", ValueError),
        ("raw_payload", b"", ValueError),
        ("records", [], TypeError),
        ("response_metadata", [], TypeError),
        ("upstream_checksum", "", ValueError),
    ],
)
def test_acquired_data_rejects_malformed_adapter_envelope(
    field_name: str,
    field_value: object,
    error_type: type[Exception],
) -> None:
    chunk = DownloadChunk(
        chunk_id="contract:klines:000001",
        data_type=DataType.KLINES,
        start=datetime(2026, 1, 1, tzinfo=UTC),
        end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        source=SourceKind.OFFICIAL_REST,
        timeframe="5m",
    )
    values: dict[str, object] = {
        "chunk": chunk,
        "source_uri": "https://example.invalid/klines",
        "raw_payload": b"[]",
        "records": (),
        "response_metadata": {},
        "upstream_checksum": None,
    }
    values[field_name] = field_value

    with pytest.raises(error_type):
        AcquiredData(**values)  # type: ignore[arg-type]


def test_download_chunk_rejects_query_bearing_expected_uri() -> None:
    with pytest.raises(ValueError, match="query or fragment"):
        DownloadChunk(
            chunk_id="contract:klines:query-uri",
            data_type=DataType.KLINES,
            start=datetime(2026, 1, 1, tzinfo=UTC),
            end=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            source=SourceKind.OFFICIAL_REST,
            timeframe="5m",
            expected_uri="https://example.invalid/klines?api_key=secret",
        )
