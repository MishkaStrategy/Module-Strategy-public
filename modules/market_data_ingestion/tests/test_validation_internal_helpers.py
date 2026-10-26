from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast

import pytest

from market_data_ingestion.canonical import sha256_canonical
from market_data_ingestion.enums import DataType, GapReason, ValidationStatus
from market_data_ingestion.records import FundingRateRecord, KlineRecord
from market_data_ingestion.validation import (
    _deduplicate,
    _interval_checks,
    apply_gap_policy,
    combine_validation_reports,
    validate_sorted_records,
)


def _clone(record: KlineRecord, **changes: object) -> KlineRecord:
    return KlineRecord(**{**record.to_row(), **changes})


def test_internal_deduplicate_retains_evidence_and_rejects_mixed_identity(
    instrument,
    five_minute_records,
) -> None:
    base = five_minute_records[0]
    conflicting = _clone(base, close="150", high="151")
    wrong_exchange = _clone(five_minute_records[1], exchange_id="other-exchange")
    wrong_market = _clone(five_minute_records[2], market_type="spot")
    wrong_symbol = _clone(five_minute_records[3], symbol="ETHUSDT")

    clean, issues, duplicates, rows_checked = _deduplicate(
        (
            wrong_symbol,
            conflicting,
            conflicting,
            base,
            wrong_market,
            base,
            wrong_exchange,
        ),
        instrument=instrument,
    )

    assert rows_checked == 7
    assert len(clean) == 1
    assert clean[0].timestamp == base.timestamp
    assert clean[0] == base or clean[0] == conflicting
    assert {issue.code for issue in issues} == {
        "mixed_exchange",
        "mixed_market",
        "mixed_symbol",
        "exact_duplicate",
        "conflicting_duplicate",
    }
    assert sorted(item.exact for item in duplicates) == [False, False, True]
    assert all(len(item.evidence_hashes) == 2 for item in duplicates)


def test_internal_interval_checks_cover_alignment_and_provisional_filters(
    five_minute_records,
) -> None:
    start = five_minute_records[0].open_time
    mixed = _clone(
        five_minute_records[0],
        timeframe="15m",
        close_time=start + timedelta(minutes=15) - timedelta(milliseconds=1),
    )
    misaligned_start = start + timedelta(minutes=1)
    misaligned = _clone(
        five_minute_records[1],
        open_time=misaligned_start,
        close_time=misaligned_start + timedelta(minutes=5) - timedelta(milliseconds=1),
    )
    provisional = _clone(five_minute_records[2], is_closed=False)

    issues, gaps, accepted = _interval_checks(
        (mixed, misaligned, provisional),
        timeframe="5m",
        allow_provisional=False,
        now=start + timedelta(minutes=12),
        expected_start=None,
        expected_end=None,
        listing_date=None,
        delisting_date=None,
    )

    assert accepted == [misaligned]
    assert not gaps
    assert {issue.code for issue in issues} == {
        "mixed_timeframe",
        "misaligned_timestamp",
        "provisional_candle",
    }


def test_internal_interval_checks_report_all_gap_positions_and_overlap(
    five_minute_records,
) -> None:
    start = five_minute_records[0].open_time
    first = five_minute_records[1]
    second = five_minute_records[3]

    issues, gaps, accepted = _interval_checks(
        (first, second, second),
        timeframe="5m",
        allow_provisional=False,
        now=datetime(2027, 1, 1, tzinfo=UTC),
        expected_start=start,
        expected_end=start + timedelta(minutes=30),
        listing_date=None,
        delisting_date=None,
    )

    assert accepted == [first, second, second]
    assert [gap.missing_intervals for gap in gaps] == [1, 1, 2]
    assert all(gap.reason is GapReason.UNKNOWN for gap in gaps)
    codes = [issue.code for issue in issues]
    assert codes.count("unresolved_gap") == 3
    assert "overlapping_interval" in codes


def test_internal_interval_checks_classify_lifetime_boundaries(
    five_minute_records,
) -> None:
    start = five_minute_records[0].open_time
    first_two = five_minute_records[:2]

    issues, gaps, accepted = _interval_checks(
        first_two,
        timeframe="5m",
        allow_provisional=False,
        now=datetime(2027, 1, 1, tzinfo=UTC),
        expected_start=start - timedelta(minutes=10),
        expected_end=start + timedelta(minutes=20),
        listing_date=start,
        delisting_date=start + timedelta(minutes=10),
    )

    assert accepted == list(first_two)
    assert [gap.reason for gap in gaps] == [
        GapReason.NOT_YET_LISTED,
        GapReason.DELISTED,
    ]
    assert {issue.code for issue in issues} == {"expected_market_unavailable"}


def test_sorted_validator_rejects_invalid_arguments_and_order(
    instrument,
    five_minute_records,
) -> None:
    start = five_minute_records[0].open_time
    with pytest.raises(TypeError, match="data_type must be DataType"):
        validate_sorted_records(
            (),
            data_type=cast(Any, "klines"),
            instrument=instrument,
            timeframe="5m",
        )
    with pytest.raises(ValueError, match="expected_start must be timezone-aware"):
        validate_sorted_records(
            (),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            expected_start=start.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="expected_end must be timezone-aware"):
        validate_sorted_records(
            (),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            expected_end=start.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="expected_start must precede"):
        validate_sorted_records(
            (),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            expected_start=start,
            expected_end=start,
        )
    with pytest.raises(ValueError, match="validation requires timeframe"):
        validate_sorted_records(
            (),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe=None,
        )
    with pytest.raises(ValueError, match="input must be ordered"):
        validate_sorted_records(
            (five_minute_records[1], five_minute_records[0]),
            data_type=DataType.KLINES,
            instrument=instrument,
            timeframe="5m",
            now=datetime(2027, 1, 1, tzinfo=UTC),
        )


def test_sorted_validator_handles_identity_duplicates_and_accept_callback(
    instrument,
    five_minute_records,
) -> None:
    accepted: list[KlineRecord] = []
    base = five_minute_records[0]
    conflict = _clone(base, close="150", high="151")
    wrong_symbol = _clone(five_minute_records[1], symbol="ETHUSDT")
    ordered = sorted(
        (base, base, conflict, conflict, wrong_symbol),
        key=lambda record: (
            record.timestamp,
            sha256_canonical(record.to_row()),
        ),
    )

    report = validate_sorted_records(
        ordered,
        data_type=DataType.KLINES,
        instrument=instrument,
        timeframe="5m",
        accept=lambda record: accepted.append(cast(KlineRecord, record)),
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )

    assert len(accepted) == 1
    assert accepted[0].timestamp == base.timestamp
    assert accepted[0] == base or accepted[0] == conflict
    assert report.rows_checked == 5
    assert report.status is ValidationStatus.REJECTED
    assert {issue.code for issue in report.issues} == {
        "exact_duplicate",
        "conflicting_duplicate",
        "mixed_symbol",
    }


def test_gap_policy_preserves_non_gap_errors_and_combines_reports(
    instrument,
    five_minute_records,
) -> None:
    start = five_minute_records[0].open_time
    funding = FundingRateRecord(
        funding_time=start,
        funding_rate="0.0001",
        mark_price="100",
        symbol=instrument.symbol,
        exchange_id=instrument.exchange_id,
        market_type=instrument.market_type.value,
        source_kind="official_rest",
    )
    funding_report = validate_sorted_records(
        (funding,),
        data_type=DataType.FUNDING_RATE,
        instrument=instrument,
        timeframe=None,
    )
    _, _, interval_records = _interval_checks(
        (five_minute_records[0], five_minute_records[2]),
        timeframe="5m",
        allow_provisional=False,
        now=datetime(2027, 1, 1, tzinfo=UTC),
        expected_start=start,
        expected_end=start + timedelta(minutes=15),
        listing_date=None,
        delisting_date=None,
    )
    interval_report = validate_sorted_records(
        interval_records,
        data_type=DataType.KLINES,
        instrument=instrument,
        timeframe="5m",
        expected_start=start,
        expected_end=start + timedelta(minutes=15),
        now=datetime(2027, 1, 1, tzinfo=UTC),
    )
    downgraded = apply_gap_policy(interval_report, reject_unresolved=False)
    combined = combine_validation_reports(
        (funding_report, downgraded),
        created_at=start,
    )

    assert downgraded.status is ValidationStatus.VALID_WITH_WARNINGS
    assert combined.created_at == start
    assert combined.rows_checked == 3
    assert combined.status is ValidationStatus.VALID_WITH_WARNINGS
