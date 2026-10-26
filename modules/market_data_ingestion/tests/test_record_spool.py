from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from market_data_ingestion.enums import DataType
from market_data_ingestion.records import (
    ExchangeMetadataRecord,
    FundingRateRecord,
    InstrumentMetadataRecord,
    OpenInterestRecord,
    PriceKlineRecord,
)
from market_data_ingestion.spool import RecordSpool


def _auxiliary_records(instrument):
    start = datetime(2026, 1, 1, tzinfo=UTC)
    common = {
        "symbol": instrument.symbol,
        "exchange_id": instrument.exchange_id,
        "market_type": instrument.market_type.value,
        "source_kind": "official_rest",
    }
    return (
        PriceKlineRecord(
            price_type=DataType.MARK_PRICE,
            open_time=start,
            close_time=start + timedelta(minutes=5) - timedelta(milliseconds=1),
            open="100",
            high="102",
            low="99",
            close="101",
            timeframe="5m",
            **common,
        ),
        FundingRateRecord(
            funding_time=start,
            funding_rate="0.0001",
            mark_price="101",
            **common,
        ),
        OpenInterestRecord(
            observed_at=start,
            sum_open_interest="10",
            sum_open_interest_value="1000",
            period="5m",
            circulating_supply="1000000",
            **common,
        ),
        InstrumentMetadataRecord(
            effective_at=start,
            canonical_id=instrument.canonical_id,
            base_asset="BTC",
            quote_asset="USDT",
            settlement_asset="USDT",
            contract_type="perpetual",
            listing_date=datetime(2020, 1, 1, tzinfo=UTC),
            delisting_date=None,
            tick_size="0.1",
            quantity_step="0.001",
            contract_multiplier="1",
            trading_status="TRADING",
            **common,
        ),
        ExchangeMetadataRecord(
            effective_at=start,
            adapter_version="1.2.0",
            maturity="tested",
            supported_markets=(instrument.market_type.value,),
            supported_data_types=(DataType.KLINES.value,),
            rate_limit_policy_json='{"requests_per_second":100}',
            **common,
        ),
    )


def test_spool_round_trips_every_record_family(
    tmp_path: Path,
    instrument,
    five_minute_records,
) -> None:
    records = (five_minute_records[0], *_auxiliary_records(instrument))
    with RecordSpool(tmp_path / "records.sqlite3", reset=True) as spool:
        assert spool.append("acquired", records) == len(records)
        for record in records:
            restored = tuple(
                spool.iter_sorted("acquired", record.data_type, record.record_timeframe)
            )
            assert restored == (record,)


def test_spool_orders_by_timestamp_and_retains_exact_duplicates(
    tmp_path: Path,
    five_minute_records,
) -> None:
    records = (
        five_minute_records[2],
        five_minute_records[0],
        five_minute_records[1],
        five_minute_records[1],
    )
    with RecordSpool(tmp_path / "records.sqlite3", reset=True) as spool:
        spool.append("acquired", records)
        restored = tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))

    assert tuple(item.timestamp for item in restored) == (
        five_minute_records[0].timestamp,
        five_minute_records[1].timestamp,
        five_minute_records[1].timestamp,
        five_minute_records[2].timestamp,
    )


def test_spool_rejects_corrupt_json_payload(
    tmp_path: Path,
    five_minute_records,
) -> None:
    path = tmp_path / "records.sqlite3"
    spool = RecordSpool(path, reset=True)
    spool.append("acquired", (five_minute_records[0],))
    spool._connection.execute("UPDATE records SET payload = ?", ("{",))
    spool._connection.commit()

    with pytest.raises(Exception) as captured:
        tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))
    spool.close(remove=True)

    assert getattr(captured.value, "code", None) == "SPOOL_PAYLOAD_CORRUPT"


def test_spool_rejects_symlink_database_path(tmp_path: Path) -> None:
    target = tmp_path / "target.sqlite3"
    target.write_text("not-a-db", encoding="utf-8")
    link = tmp_path / "link.sqlite3"
    link.symlink_to(target)

    with pytest.raises(Exception) as captured:
        RecordSpool(link)

    assert getattr(captured.value, "code", None) == "SPOOL_PATH_UNSAFE"


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("identity", "0" * 64),
        ("timestamp", "2026-01-02T00:00:00+00:00"),
        ("end_time", "2026-01-02T00:05:00+00:00"),
    ],
)
def test_spool_detects_index_metadata_tamper(
    tmp_path: Path,
    five_minute_records,
    column: str,
    value: str,
) -> None:
    path = tmp_path / f"tamper-{column}.sqlite3"
    spool = RecordSpool(path, reset=True)
    spool.append("acquired", (five_minute_records[0],))
    spool._connection.execute(f"UPDATE records SET {column} = ?", (value,))
    spool._connection.commit()

    with pytest.raises(Exception) as captured:
        tuple(spool.iter_sorted("acquired", DataType.KLINES, "5m"))
    spool.close(remove=True)

    assert getattr(captured.value, "code", None) == "SPOOL_METADATA_CORRUPT"


def test_spool_rejects_unsupported_existing_schema_version(tmp_path: Path) -> None:
    path = tmp_path / "unsupported.sqlite3"
    spool = RecordSpool(path, reset=True)
    spool._connection.execute(
        "UPDATE spool_metadata SET value = '999' WHERE key = 'schema_version'"
    )
    spool._connection.commit()
    spool.close()

    with pytest.raises(Exception) as captured:
        RecordSpool(path)

    assert getattr(captured.value, "code", None) == "SPOOL_SCHEMA_UNSUPPORTED"


def test_spool_wraps_non_database_file(tmp_path: Path) -> None:
    path = tmp_path / "not-database.sqlite3"
    path.write_bytes(b"not a sqlite database")

    with pytest.raises(Exception) as captured:
        RecordSpool(path)

    assert getattr(captured.value, "code", None) == "SPOOL_OPEN_FAILED"


def test_spool_rejects_existing_table_with_wrong_columns(tmp_path: Path) -> None:
    import sqlite3

    path = tmp_path / "wrong-columns.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE spool_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    connection.execute("INSERT INTO spool_metadata(key, value) VALUES('schema_version', '1')")
    connection.execute("CREATE TABLE records (payload TEXT NOT NULL)")
    connection.commit()
    connection.close()

    with pytest.raises(Exception) as captured:
        RecordSpool(path)

    assert getattr(captured.value, "code", None) == "SPOOL_OPEN_FAILED"
