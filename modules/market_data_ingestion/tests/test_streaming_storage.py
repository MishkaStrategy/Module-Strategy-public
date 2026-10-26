from __future__ import annotations

import csv
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from market_data_ingestion.enums import DataType
from market_data_ingestion.errors import AdapterError
from market_data_ingestion.storage import LocalStorage


def test_csv_partition_starts_writing_before_exhausting_input(
    tmp_path: Path,
    instrument,
    five_minute_records,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    storage = LocalStorage(tmp_path / "data")
    state = {"header_written": False}
    original = csv.DictWriter.writeheader

    def tracked_writeheader(self):
        state["header_written"] = True
        return original(self)

    monkeypatch.setattr(csv.DictWriter, "writeheader", tracked_writeheader)

    class GuardedIterator:
        def __init__(self) -> None:
            self._index = 0

        def __iter__(self) -> GuardedIterator:
            return self

        def __next__(self):
            if self._index >= len(five_minute_records):
                raise StopIteration
            if self._index > 0 and not state["header_written"]:
                raise AssertionError("input was exhausted before CSV output started")
            value = five_minute_records[self._index]
            self._index += 1
            return value

    descriptor = storage.write_partition(
        dataset_id="streaming",
        version=1,
        layer="normalized",
        instrument=instrument,
        data_type=DataType.KLINES,
        timeframe="5m",
        records=GuardedIterator(),
        source_artifact_ids=("raw:test",),
        format_name="csv",
    )

    assert descriptor.row_count == len(five_minute_records)
    assert state["header_written"] is True
    assert (storage.root / descriptor.storage_uri).is_file()


def test_streaming_csv_failure_removes_unpublished_temporary_file(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    storage = LocalStorage(tmp_path / "data")

    def failing_records() -> Iterator[object]:
        yield five_minute_records[0]
        raise RuntimeError("injected normalization failure")

    with pytest.raises(RuntimeError, match="injected normalization failure"):
        storage.write_partition(
            dataset_id="streaming-failure",
            version=1,
            layer="normalized",
            instrument=instrument,
            data_type=DataType.KLINES,
            timeframe="5m",
            records=failing_records(),
            source_artifact_ids=("raw:test",),
            format_name="csv",
        )

    assert not tuple(storage.root.glob("normalized/**/*.csv"))
    assert not tuple(storage.root.glob("normalized/**/*.tmp"))


def test_monthly_partition_rejects_cross_month_records(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    storage = LocalStorage(tmp_path / "data")
    first = five_minute_records[0]
    cross_month = type(first)(
        open_time=first.open_time + timedelta(days=31),
        close_time=first.close_time + timedelta(days=31),
        open=first.open,
        high=first.high,
        low=first.low,
        close=first.close,
        volume=first.volume,
        quote_volume=first.quote_volume,
        trade_count=first.trade_count,
        taker_buy_base_volume=first.taker_buy_base_volume,
        taker_buy_quote_volume=first.taker_buy_quote_volume,
        symbol=first.symbol,
        timeframe=first.timeframe,
        exchange_id=first.exchange_id,
        market_type=first.market_type,
        source_kind=first.source_kind,
    )

    with pytest.raises(Exception) as captured:
        storage.write_partition(
            dataset_id="month-mix",
            version=1,
            layer="normalized",
            instrument=instrument,
            data_type=DataType.KLINES,
            timeframe="5m",
            records=(first, cross_month),
            source_artifact_ids=("raw:test",),
            format_name="csv",
        )

    assert getattr(captured.value, "code", None) == "MONTH_MIX"


def test_streaming_pipeline_allows_total_rows_above_per_chunk_memory_bound(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime, timedelta

    from market_data_ingestion.adapters.fake import FakeExchangeAdapter
    from market_data_ingestion.contracts import MarketDataRequest
    from market_data_ingestion.enums import MarketType
    from market_data_ingestion.pipeline import IngestionService
    from market_data_ingestion.registry import SQLiteDatasetRegistry

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    request = MarketDataRequest(
        request_id="streaming-total-above-bound",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=("15m",),
        start=start,
        end=start + timedelta(hours=1),
    )

    result = IngestionService(
        adapters={"fake": FakeExchangeAdapter(chunk_bars=2)},
        storage=storage,
        registry=registry,
    ).execute(
        request,
        format_name="csv",
        max_records_in_memory=3,
    )

    assert result.dataset is not None
    assert not tuple((storage.root / "staging").glob("*.sqlite3*"))


def test_streaming_pipeline_removes_spool_after_adapter_failure(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    from market_data_ingestion.adapters.fake import FakeExchangeAdapter
    from market_data_ingestion.contracts import MarketDataRequest
    from market_data_ingestion.enums import MarketType
    from market_data_ingestion.pipeline import IngestionService
    from market_data_ingestion.registry import SQLiteDatasetRegistry

    storage = LocalStorage(tmp_path / "data")
    registry = SQLiteDatasetRegistry(storage.root / "registry.sqlite3")
    start = datetime(2026, 1, 1, tzinfo=UTC)
    request = MarketDataRequest(
        request_id="streaming-cleanup-failure",
        exchange_id="fake",
        market_type=MarketType.USDT_M_PERPETUAL,
        symbol="BTCUSDT",
        required_data=(DataType.KLINES,),
        optional_data=(),
        base_timeframe="5m",
        derived_timeframes=(),
        start=start,
        end=start + timedelta(minutes=20),
    )
    failing_chunk = f"{request.request_id}:klines:000002"

    with pytest.raises(AdapterError):
        IngestionService(
            adapters={
                "fake": FakeExchangeAdapter(
                    chunk_bars=2,
                    fail_chunks=frozenset({failing_chunk}),
                )
            },
            storage=storage,
            registry=registry,
        ).execute(request, format_name="csv", max_records_in_memory=3)

    assert not tuple((storage.root / "staging").glob("*.sqlite3*"))


def test_streaming_csv_partition_round_trip_is_typed_and_canonical(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    storage = LocalStorage(tmp_path / "data")
    descriptor = storage.write_partition(
        dataset_id="round-trip",
        version=1,
        layer="normalized",
        instrument=instrument,
        data_type=DataType.KLINES,
        timeframe="5m",
        records=iter(five_minute_records),
        source_artifact_ids=("raw:test",),
        format_name="csv",
    )

    payload = (storage.root / descriptor.storage_uri).read_text(encoding="utf-8")
    restored = tuple(storage.iter_partition_records(descriptor))

    assert restored == five_minute_records
    assert ",true\n" in payload
    assert "True" not in payload


def test_metadata_csv_arrays_use_json_and_round_trip(
    tmp_path: Path,
    instrument,
) -> None:
    from datetime import UTC, datetime

    from market_data_ingestion.records import ExchangeMetadataRecord

    storage = LocalStorage(tmp_path / "data")
    record = ExchangeMetadataRecord(
        effective_at=datetime(2026, 1, 1, tzinfo=UTC),
        symbol=instrument.symbol,
        exchange_id=instrument.exchange_id,
        market_type=instrument.market_type.value,
        source_kind="official_rest",
        adapter_version="1.2.0",
        maturity="tested",
        supported_markets=(instrument.market_type.value, "spot"),
        supported_data_types=("funding_rate", "klines"),
        rate_limit_policy_json='{"requests_per_second":100}',
    )
    descriptor = storage.write_partition(
        dataset_id="metadata-round-trip",
        version=1,
        layer="normalized",
        instrument=instrument,
        data_type=DataType.EXCHANGE_METADATA,
        timeframe=None,
        records=(record,),
        source_artifact_ids=("raw:metadata",),
        format_name="csv",
    )

    payload = (storage.root / descriptor.storage_uri).read_text(encoding="utf-8")
    restored = tuple(storage.iter_partition_records(descriptor))

    assert restored == (record,)
    assert "['" not in payload
    assert '"[""spot""' in payload


def _descriptor_with_current_checksum(storage, descriptor):  # type: ignore[no-untyped-def]
    from dataclasses import replace

    from market_data_ingestion.canonical import sha256_file

    return replace(
        descriptor,
        checksum_sha256=sha256_file(storage.root / descriptor.storage_uri),
    )


@pytest.mark.parametrize("mutation", ["missing", "extra"])
def test_partition_reader_rejects_csv_header_schema_drift(
    tmp_path: Path,
    instrument,
    five_minute_records,
    mutation: str,
) -> None:
    storage = LocalStorage(tmp_path / mutation)
    descriptor = storage.write_partition(
        dataset_id=f"schema-{mutation}",
        version=1,
        layer="normalized",
        instrument=instrument,
        data_type=DataType.KLINES,
        timeframe="5m",
        records=five_minute_records,
        source_artifact_ids=("raw:test",),
        format_name="csv",
    )
    path = storage.root / descriptor.storage_uri
    lines = path.read_text(encoding="utf-8").splitlines()
    header = lines[0].split(",")
    if mutation == "missing":
        header.pop()
    else:
        header.append("unexpected_column")
    lines[0] = ",".join(header)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tampered = _descriptor_with_current_checksum(storage, descriptor)

    with pytest.raises(Exception) as captured:
        tuple(storage.iter_partition_records(tampered))

    assert getattr(captured.value, "code", None) == "PARTITION_SCHEMA_MISMATCH"


def test_partition_reader_wraps_invalid_csv_row_as_storage_error(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    storage = LocalStorage(tmp_path / "invalid-row")
    descriptor = storage.write_partition(
        dataset_id="invalid-row",
        version=1,
        layer="normalized",
        instrument=instrument,
        data_type=DataType.KLINES,
        timeframe="5m",
        records=five_minute_records,
        source_artifact_ids=("raw:test",),
        format_name="csv",
    )
    path = storage.root / descriptor.storage_uri
    rows = list(csv.reader(path.read_text(encoding="utf-8").splitlines()))
    is_closed_index = rows[0].index("is_closed")
    rows[1][is_closed_index] = "not-a-boolean"
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    tampered = _descriptor_with_current_checksum(storage, descriptor)

    with pytest.raises(Exception) as captured:
        tuple(storage.iter_partition_records(tampered))

    assert getattr(captured.value, "code", None) == "PARTITION_ROW_INVALID"
    assert getattr(captured.value, "details", {}).get("row_number") == 2
