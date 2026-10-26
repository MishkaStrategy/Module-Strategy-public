from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from market_data_ingestion.enums import DataType, GapReason, ValidationStatus
from market_data_ingestion.records import FundingRateRecord, KlineRecord
from market_data_ingestion.validation import validate_klines, validate_records


def test_exact_duplicate_is_removed_with_evidence(instrument, five_minute_records) -> None:
    clean, report = validate_klines(
        (*five_minute_records, five_minute_records[2]),
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert len(clean) == len(five_minute_records)
    assert report.status is ValidationStatus.VALID_WITH_WARNINGS
    assert report.duplicates[0].exact is True


def test_conflicting_duplicate_rejects(instrument, five_minute_records) -> None:
    original = five_minute_records[2]
    conflicting = KlineRecord(**{**original.to_row(), "close": "999", "high": "1000"})
    _, report = validate_klines(
        (*five_minute_records, conflicting),
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert report.status is ValidationStatus.REJECTED
    assert any(item.code == "conflicting_duplicate" for item in report.issues)


def test_gap_is_invalid(instrument, five_minute_records) -> None:
    records = five_minute_records[:3] + five_minute_records[4:]
    _, report = validate_klines(
        records,
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    assert report.status is ValidationStatus.INVALID
    assert report.gaps[0].missing_intervals == 1


def test_provisional_is_rejected(instrument, five_minute_records) -> None:
    record = five_minute_records[0]
    provisional = KlineRecord(**{**record.to_row(), "is_closed": False})
    _, report = validate_klines(
        (provisional,),
        instrument=instrument,
        timeframe="5m",
        now=record.open_time + timedelta(minutes=1),
    )
    assert report.status is ValidationStatus.INVALID
    assert any(item.code == "provisional_candle" for item in report.issues)


def test_leading_and_trailing_range_gaps_are_detected(instrument, five_minute_records) -> None:
    records = five_minute_records[1:-1]
    _, report = validate_klines(
        records,
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
        expected_start=five_minute_records[0].open_time,
        expected_end=five_minute_records[-1].open_time + timedelta(minutes=5),
    )
    assert report.status is ValidationStatus.INVALID
    assert [(gap.start, gap.end, gap.missing_intervals) for gap in report.gaps] == [
        (
            five_minute_records[0].open_time,
            five_minute_records[1].open_time,
            1,
        ),
        (
            five_minute_records[-1].open_time,
            five_minute_records[-1].open_time + timedelta(minutes=5),
            1,
        ),
    ]


def test_gap_policy_downgrades_only_explicit_gap_errors(instrument, five_minute_records) -> None:
    from market_data_ingestion.validation import apply_gap_policy

    records = five_minute_records[:3] + five_minute_records[4:]
    _, report = validate_klines(
        records,
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    accepted = apply_gap_policy(report, reject_unresolved=False)
    assert accepted.status is ValidationStatus.VALID_WITH_WARNINGS
    assert accepted.gaps == report.gaps
    assert all(
        issue.severity.value != "ERROR"
        for issue in accepted.issues
        if issue.code == "unresolved_gap"
    )


def test_gap_before_listing_is_classified_as_expected_absence(
    instrument, five_minute_records
) -> None:
    from market_data_ingestion.enums import GapReason

    first = five_minute_records[0].open_time
    listing = first
    _, report = validate_klines(
        five_minute_records,
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
        expected_start=first - timedelta(minutes=10),
        expected_end=five_minute_records[-1].open_time + timedelta(minutes=5),
        listing_date=listing,
    )
    assert report.status is ValidationStatus.VALID
    assert report.gaps[0].reason is GapReason.NOT_YET_LISTED
    assert any(issue.code == "expected_market_unavailable" for issue in report.issues)
    assert not any(issue.code == "unresolved_gap" for issue in report.issues)


def test_gap_after_delisting_is_classified_as_expected_absence(
    instrument, five_minute_records
) -> None:
    from market_data_ingestion.enums import GapReason

    delisting = five_minute_records[-1].open_time + timedelta(minutes=5)
    _, report = validate_klines(
        five_minute_records,
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
        expected_start=five_minute_records[0].open_time,
        expected_end=delisting + timedelta(minutes=10),
        delisting_date=delisting,
    )
    assert report.status is ValidationStatus.VALID
    assert report.gaps[-1].reason is GapReason.DELISTED
    assert any(issue.code == "expected_market_unavailable" for issue in report.issues)


def test_sorted_stream_validation_matches_materialized_validation(
    instrument, five_minute_records
) -> None:
    from market_data_ingestion.canonical import sha256_canonical
    from market_data_ingestion.enums import DataType
    from market_data_ingestion.validation import (
        validate_records,
        validate_sorted_records,
    )

    source = (
        five_minute_records[2],
        five_minute_records[0],
        five_minute_records[1],
        five_minute_records[1],
    )
    expected_records, expected_report = validate_records(
        source,
        data_type=DataType.KLINES,
        instrument=instrument,
        timeframe="5m",
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    accepted = []
    ordered = sorted(
        source,
        key=lambda item: (item.timestamp, sha256_canonical(item.to_row())),
    )
    actual_report = validate_sorted_records(
        ordered,
        data_type=DataType.KLINES,
        instrument=instrument,
        timeframe="5m",
        accept=accepted.append,
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert tuple(accepted) == expected_records
    assert actual_report.status == expected_report.status
    assert actual_report.issues == expected_report.issues
    assert actual_report.gaps == expected_report.gaps
    assert actual_report.duplicates == expected_report.duplicates
    assert actual_report.rows_checked == expected_report.rows_checked


def test_empty_interval_range_is_reported_as_one_complete_gap(instrument) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    _, report = validate_klines(
        (),
        instrument=instrument,
        timeframe="5m",
        expected_start=start,
        expected_end=start + timedelta(minutes=20),
    )

    assert report.status is ValidationStatus.INVALID
    assert report.rows_checked == 0
    assert len(report.gaps) == 1
    assert report.gaps[0].start == start
    assert report.gaps[0].end == start + timedelta(minutes=20)
    assert report.gaps[0].missing_intervals == 4
    assert report.gaps[0].reason is GapReason.UNKNOWN
    assert any(issue.code == "unresolved_gap" for issue in report.issues)


def test_empty_range_before_listing_is_expected_absence(instrument) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    listing = start + timedelta(hours=1)
    _, report = validate_klines(
        (),
        instrument=instrument,
        timeframe="5m",
        expected_start=start,
        expected_end=start + timedelta(minutes=20),
        listing_date=listing,
    )

    assert report.status is ValidationStatus.VALID
    assert report.gaps[0].reason is GapReason.NOT_YET_LISTED
    assert report.issues[0].code == "expected_market_unavailable"


def test_empty_interval_stream_without_range_is_invalid(instrument) -> None:
    _, report = validate_klines(
        (),
        instrument=instrument,
        timeframe="5m",
    )

    assert report.status is ValidationStatus.INVALID
    assert not report.gaps
    assert report.issues[0].code == "empty_interval_stream"


@pytest.mark.parametrize("boundary", ["start", "end"])
def test_expected_range_boundaries_must_align_to_timeframe(
    instrument,
    boundary: str,
) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    kwargs = {
        "expected_start": start,
        "expected_end": start + timedelta(minutes=10),
    }
    kwargs[f"expected_{boundary}"] += timedelta(minutes=1)

    with pytest.raises(ValueError, match=f"expected_{boundary} must align"):
        validate_klines(
            (),
            instrument=instrument,
            timeframe="5m",
            **kwargs,
        )


def test_interval_records_outside_exclusive_expected_range_are_rejected(
    instrument,
    five_minute_records,
) -> None:
    expected_start = five_minute_records[1].open_time
    expected_end = five_minute_records[3].open_time
    accepted, report = validate_klines(
        five_minute_records[:4],
        instrument=instrument,
        timeframe="5m",
        expected_start=expected_start,
        expected_end=expected_end,
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert accepted == five_minute_records[1:3]
    assert report.status is ValidationStatus.REJECTED
    assert {issue.code for issue in report.issues} >= {
        "record_before_expected_range",
        "record_after_expected_range",
    }


def test_point_events_use_same_exclusive_expected_range(instrument) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)

    def funding(at: datetime) -> FundingRateRecord:
        return FundingRateRecord(
            funding_time=at,
            funding_rate="0.0001",
            mark_price="100",
            symbol=instrument.symbol,
            exchange_id=instrument.exchange_id,
            market_type=instrument.market_type.value,
            source_kind="official_rest",
        )

    accepted, report = validate_records(
        (
            funding(start - timedelta(hours=8)),
            funding(start),
            funding(start + timedelta(hours=8)),
        ),
        data_type=DataType.FUNDING_RATE,
        instrument=instrument,
        timeframe=None,
        expected_start=start,
        expected_end=start + timedelta(hours=8),
    )

    assert tuple(record.timestamp for record in accepted) == (start,)
    assert report.status is ValidationStatus.REJECTED
    assert {issue.code for issue in report.issues} >= {
        "record_before_expected_range",
        "record_after_expected_range",
    }
