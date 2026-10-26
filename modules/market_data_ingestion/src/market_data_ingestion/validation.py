from __future__ import annotations

from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

from .canonical import sha256_canonical
from .contracts import (
    DataValidationReport,
    DuplicateDescriptor,
    GapDescriptor,
    InstrumentReference,
    ValidationIssue,
)
from .enums import DataType, GapReason, Severity, ValidationStatus
from .records import (
    FundingRateRecord,
    KlineRecord,
    NormalizedRecord,
    OpenInterestRecord,
    PriceKlineRecord,
)
from .timeframes import is_timeframe_aligned, timeframe_delta

_INTERVAL_RECORDS = (KlineRecord, PriceKlineRecord, OpenInterestRecord)


def _record_identity(record: NormalizedRecord) -> str:
    return sha256_canonical(record.to_row())


def _status(
    issues: Sequence[ValidationIssue],
    duplicates: Sequence[DuplicateDescriptor],
) -> ValidationStatus:
    severities = {issue.severity for issue in issues}
    if Severity.CRITICAL in severities:
        return ValidationStatus.REJECTED
    if Severity.ERROR in severities:
        return ValidationStatus.INVALID
    if Severity.WARNING in severities or duplicates:
        return ValidationStatus.VALID_WITH_WARNINGS
    return ValidationStatus.VALID


def apply_gap_policy(
    report: DataValidationReport, *, reject_unresolved: bool
) -> DataValidationReport:
    """Apply the request quality gate without hiding gap evidence.

    Unknown gaps remain in the report. When the caller explicitly allows an
    incomplete required stream, only ``unresolved_gap`` issues are downgraded
    from ERROR to WARNING; every other validation error remains fail-closed.
    """

    if reject_unresolved or not report.gaps:
        return report
    issues = tuple(
        (
            ValidationIssue(
                code=issue.code,
                severity=Severity.WARNING,
                message=issue.message,
                start=issue.start,
                end=issue.end,
                evidence=dict(issue.evidence),
            )
            if issue.code == "unresolved_gap" and issue.severity is Severity.ERROR
            else issue
        )
        for issue in report.issues
    )
    return DataValidationReport(
        status=_status(issues, report.duplicates),
        issues=issues,
        gaps=report.gaps,
        duplicates=report.duplicates,
        rows_checked=report.rows_checked,
        created_at=report.created_at,
        report_version=report.report_version,
    )


def classify_gap_reason(
    start: datetime,
    end: datetime,
    *,
    listing_date: datetime | None = None,
    delisting_date: datetime | None = None,
) -> GapReason:
    """Classify ranges proven to be outside the instrument trading lifetime."""

    if listing_date is not None and end <= listing_date:
        return GapReason.NOT_YET_LISTED
    if delisting_date is not None and start >= delisting_date:
        return GapReason.DELISTED
    return GapReason.UNKNOWN


def _append_gap_evidence(
    *,
    gaps: list[GapDescriptor],
    issues: list[ValidationIssue],
    start: datetime,
    end: datetime,
    timeframe: str,
    missing_intervals: int,
    evidence: tuple[str, ...],
    listing_date: datetime | None,
    delisting_date: datetime | None,
    position: str,
) -> None:
    reason = classify_gap_reason(
        start,
        end,
        listing_date=listing_date,
        delisting_date=delisting_date,
    )
    gaps.append(
        GapDescriptor(
            start=start,
            end=end,
            timeframe=timeframe,
            missing_intervals=missing_intervals,
            reason=reason,
            evidence=evidence,
        )
    )
    known_absence = reason in {GapReason.NOT_YET_LISTED, GapReason.DELISTED}
    issues.append(
        ValidationIssue(
            code=("expected_market_unavailable" if known_absence else "unresolved_gap"),
            severity=(Severity.INFO if known_absence else Severity.ERROR),
            message=(
                f"classified {missing_intervals} {position} interval(s) as {reason.value}"
                if known_absence
                else f"detected {missing_intervals} {position} missing interval(s)"
            ),
            start=start,
            end=end,
            evidence={"gap_reason": reason.value},
        )
    )


def _identity_issues(
    record: NormalizedRecord,
    instrument: InstrumentReference,
) -> tuple[ValidationIssue, ...]:
    issues: list[ValidationIssue] = []
    if record.exchange_id != instrument.exchange_id:
        issues.append(
            ValidationIssue(
                code="mixed_exchange",
                severity=Severity.CRITICAL,
                message="record exchange does not match resolved instrument",
                start=record.timestamp,
            )
        )
    if record.market_type != instrument.market_type.value:
        issues.append(
            ValidationIssue(
                code="mixed_market",
                severity=Severity.CRITICAL,
                message="record market type does not match resolved instrument",
                start=record.timestamp,
            )
        )
    if record.symbol != instrument.symbol:
        issues.append(
            ValidationIssue(
                code="mixed_symbol",
                severity=Severity.CRITICAL,
                message="record symbol does not match resolved instrument",
                start=record.timestamp,
            )
        )
    return tuple(issues)


def _deduplicate[RecordT: NormalizedRecord](
    records: Iterable[RecordT],
    *,
    instrument: InstrumentReference,
) -> tuple[list[RecordT], list[ValidationIssue], list[DuplicateDescriptor], int]:
    ordered = sorted(tuple(records), key=lambda item: (item.timestamp, _record_identity(item)))
    issues: list[ValidationIssue] = []
    duplicates: list[DuplicateDescriptor] = []
    clean: list[RecordT] = []
    by_timestamp: dict[datetime, RecordT] = {}
    for record in ordered:
        identity_issues = _identity_issues(record, instrument)
        if identity_issues:
            issues.extend(identity_issues)
            continue
        existing = by_timestamp.get(record.timestamp)
        if existing is not None:
            existing_hash = _record_identity(existing)
            current_hash = _record_identity(record)
            exact = existing_hash == current_hash
            duplicates.append(
                DuplicateDescriptor(
                    timestamp=record.timestamp,
                    exact=exact,
                    source_partition_ids=(),
                    evidence_hashes=(existing_hash, current_hash),
                )
            )
            if exact:
                issues.append(
                    ValidationIssue(
                        code="exact_duplicate",
                        severity=Severity.INFO,
                        message="exact duplicate removed with evidence retained",
                        start=record.timestamp,
                    )
                )
                continue
            issues.append(
                ValidationIssue(
                    code="conflicting_duplicate",
                    severity=Severity.CRITICAL,
                    message="same record timestamp has conflicting content",
                    start=record.timestamp,
                )
            )
            continue
        by_timestamp[record.timestamp] = record
        clean.append(record)
    return clean, issues, duplicates, len(ordered)


def _interval_checks(
    records: Sequence[NormalizedRecord],
    *,
    timeframe: str,
    allow_provisional: bool,
    now: datetime,
    expected_start: datetime | None,
    expected_end: datetime | None,
    listing_date: datetime | None,
    delisting_date: datetime | None,
) -> tuple[list[ValidationIssue], list[GapDescriptor], list[NormalizedRecord]]:
    expected_step = timeframe_delta(timeframe)
    issues: list[ValidationIssue] = []
    gaps: list[GapDescriptor] = []
    accepted: list[NormalizedRecord] = []
    for record in records:
        if record.record_timeframe != timeframe:
            issues.append(
                ValidationIssue(
                    code="mixed_timeframe",
                    severity=Severity.CRITICAL,
                    message="record timeframe does not match requested timeframe",
                    start=record.timestamp,
                )
            )
            continue
        if not is_timeframe_aligned(record.timestamp, timeframe):
            issues.append(
                ValidationIssue(
                    code="misaligned_timestamp",
                    severity=Severity.ERROR,
                    message="record timestamp is not aligned to the UTC timeframe boundary",
                    start=record.timestamp,
                )
            )
        if (
            isinstance(record, (KlineRecord, PriceKlineRecord))
            and not allow_provisional
            and (not record.is_closed or record.end_time >= now)
        ):
            issues.append(
                ValidationIssue(
                    code="provisional_candle",
                    severity=Severity.ERROR,
                    message="unfinished candle is not allowed in a confirmed dataset",
                    start=record.timestamp,
                    end=record.end_time,
                )
            )
            continue
        accepted.append(record)

    if accepted and expected_start is not None:
        leading_delta = accepted[0].timestamp - expected_start
        leading_missing = int(leading_delta // expected_step)
        if leading_missing > 0:
            gap_start = accepted[0].timestamp - leading_missing * expected_step
            _append_gap_evidence(
                gaps=gaps,
                issues=issues,
                start=gap_start,
                end=accepted[0].timestamp,
                timeframe=timeframe,
                missing_intervals=leading_missing,
                evidence=(
                    f"requested_start={expected_start.isoformat()}",
                    f"first_record={accepted[0].timestamp.isoformat()}",
                ),
                listing_date=listing_date,
                delisting_date=delisting_date,
                position="leading",
            )

    previous: NormalizedRecord | None = None
    for record in accepted:
        if previous is not None:
            delta = record.timestamp - previous.timestamp
            if delta > expected_step:
                missing = int(delta / expected_step) - 1
                gap_start = previous.timestamp + expected_step
                _append_gap_evidence(
                    gaps=gaps,
                    issues=issues,
                    start=gap_start,
                    end=record.timestamp,
                    timeframe=timeframe,
                    missing_intervals=missing,
                    evidence=(
                        previous.timestamp.isoformat(),
                        record.timestamp.isoformat(),
                    ),
                    listing_date=listing_date,
                    delisting_date=delisting_date,
                    position="internal",
                )
            elif delta < expected_step:
                issues.append(
                    ValidationIssue(
                        code="overlapping_interval",
                        severity=Severity.CRITICAL,
                        message="record intervals overlap or advance by less than one timeframe",
                        start=previous.timestamp,
                        end=record.timestamp,
                    )
                )
        previous = record

    if accepted and expected_end is not None:
        first_missing = accepted[-1].timestamp + expected_step
        trailing_delta = expected_end - first_missing
        trailing_missing = int(trailing_delta // expected_step)
        if trailing_missing > 0:
            gap_end = first_missing + trailing_missing * expected_step
            _append_gap_evidence(
                gaps=gaps,
                issues=issues,
                start=first_missing,
                end=gap_end,
                timeframe=timeframe,
                missing_intervals=trailing_missing,
                evidence=(
                    f"last_record={accepted[-1].timestamp.isoformat()}",
                    f"requested_end={expected_end.isoformat()}",
                ),
                listing_date=listing_date,
                delisting_date=delisting_date,
                position="trailing",
            )
    return issues, gaps, accepted


def validate_sorted_records(
    records: Iterable[NormalizedRecord],
    *,
    data_type: DataType,
    instrument: InstrumentReference,
    timeframe: str | None,
    accept: Callable[[NormalizedRecord], None] | None = None,
    allow_provisional: bool = False,
    now: datetime | None = None,
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
    listing_date: datetime | None = None,
    delisting_date: datetime | None = None,
) -> DataValidationReport:
    """Validate a deterministic timestamp/identity ordered stream in one pass."""

    if not isinstance(data_type, DataType):
        raise TypeError("data_type must be DataType")
    current_time = (now or datetime.now(UTC)).astimezone(UTC)
    if expected_start is not None:
        if expected_start.tzinfo is None or expected_start.utcoffset() is None:
            raise ValueError("expected_start must be timezone-aware")
        expected_start = expected_start.astimezone(UTC)
    if expected_end is not None:
        if expected_end.tzinfo is None or expected_end.utcoffset() is None:
            raise ValueError("expected_end must be timezone-aware")
        expected_end = expected_end.astimezone(UTC)
    if expected_start is not None and expected_end is not None and expected_start >= expected_end:
        raise ValueError("expected_start must precede expected_end")
    interval_stream = data_type in {
        DataType.KLINES,
        DataType.MARK_PRICE,
        DataType.INDEX_PRICE,
        DataType.PREMIUM_INDEX,
        DataType.OPEN_INTEREST_HISTORY,
    }
    if interval_stream and timeframe is None:
        raise ValueError(f"{data_type.value} validation requires timeframe")
    expected_step = timeframe_delta(timeframe) if timeframe is not None else None
    if interval_stream and timeframe is not None:
        if expected_start is not None and not is_timeframe_aligned(
            expected_start,
            timeframe,
        ):
            raise ValueError("expected_start must align to timeframe")
        if expected_end is not None and not is_timeframe_aligned(
            expected_end,
            timeframe,
        ):
            raise ValueError("expected_end must align to timeframe")

    issues: list[ValidationIssue] = []
    gaps: list[GapDescriptor] = []
    duplicates: list[DuplicateDescriptor] = []
    rows_checked = 0
    previous_sort_key: tuple[datetime, str] | None = None
    timestamp_record: NormalizedRecord | None = None
    previous_accepted: NormalizedRecord | None = None
    first_accepted: NormalizedRecord | None = None

    for record in records:
        rows_checked += 1
        if record.data_type is not data_type:
            raise TypeError("records must all match data_type")
        identity = _record_identity(record)
        sort_key = (record.timestamp, identity)
        if previous_sort_key is not None and sort_key < previous_sort_key:
            raise ValueError(
                "validate_sorted_records input must be ordered by timestamp and identity"
            )
        previous_sort_key = sort_key
        identity_issues = _identity_issues(record, instrument)
        if identity_issues:
            issues.extend(identity_issues)
            continue
        if timestamp_record is not None and timestamp_record.timestamp == record.timestamp:
            existing_hash = _record_identity(timestamp_record)
            exact = existing_hash == identity
            duplicates.append(
                DuplicateDescriptor(
                    timestamp=record.timestamp,
                    exact=exact,
                    source_partition_ids=(),
                    evidence_hashes=(existing_hash, identity),
                )
            )
            issues.append(
                ValidationIssue(
                    code="exact_duplicate" if exact else "conflicting_duplicate",
                    severity=Severity.INFO if exact else Severity.CRITICAL,
                    message=(
                        "exact duplicate removed with evidence retained"
                        if exact
                        else "same record timestamp has conflicting content"
                    ),
                    start=record.timestamp,
                )
            )
            continue
        timestamp_record = record

        accepted = True
        if expected_start is not None and record.timestamp < expected_start:
            issues.append(
                ValidationIssue(
                    code="record_before_expected_range",
                    severity=Severity.CRITICAL,
                    message="record timestamp precedes the expected source range",
                    start=record.timestamp,
                    end=expected_start,
                )
            )
            accepted = False
        if expected_end is not None and record.timestamp >= expected_end:
            issues.append(
                ValidationIssue(
                    code="record_after_expected_range",
                    severity=Severity.CRITICAL,
                    message="record timestamp is outside the exclusive range end",
                    start=record.timestamp,
                    end=expected_end,
                )
            )
            accepted = False
        if interval_stream:
            if record.record_timeframe != timeframe:
                issues.append(
                    ValidationIssue(
                        code="mixed_timeframe",
                        severity=Severity.CRITICAL,
                        message="record timeframe does not match requested timeframe",
                        start=record.timestamp,
                    )
                )
                accepted = False
            elif timeframe is not None and not is_timeframe_aligned(record.timestamp, timeframe):
                issues.append(
                    ValidationIssue(
                        code="misaligned_timestamp",
                        severity=Severity.ERROR,
                        message=("record timestamp is not aligned to the UTC timeframe boundary"),
                        start=record.timestamp,
                    )
                )
            if isinstance(record, (KlineRecord, PriceKlineRecord)) and (
                not allow_provisional and (not record.is_closed or record.end_time >= current_time)
            ):
                issues.append(
                    ValidationIssue(
                        code="provisional_candle",
                        severity=Severity.ERROR,
                        message=("unfinished candle is not allowed in a confirmed dataset"),
                        start=record.timestamp,
                        end=record.end_time,
                    )
                )
                accepted = False

        if data_type is DataType.KLINES:
            if not isinstance(record, KlineRecord):
                raise TypeError("klines must contain KlineRecord")
            open_price, high, low, close, volume = (
                Decimal(record.open),
                Decimal(record.high),
                Decimal(record.low),
                Decimal(record.close),
                Decimal(record.volume),
            )
            if high < max(open_price, close, low) or low > min(open_price, close, high):
                issues.append(
                    ValidationIssue(
                        code="ohlc_invariant",
                        severity=Severity.CRITICAL,
                        message="OHLC geometry is invalid",
                        start=record.open_time,
                    )
                )
            if volume < 0:
                issues.append(
                    ValidationIssue(
                        code="negative_volume",
                        severity=Severity.CRITICAL,
                        message="volume must be non-negative",
                        start=record.open_time,
                    )
                )
        elif data_type in {
            DataType.MARK_PRICE,
            DataType.INDEX_PRICE,
            DataType.PREMIUM_INDEX,
        }:
            if not isinstance(record, PriceKlineRecord):
                raise TypeError("price data must contain PriceKlineRecord")
            if record.data_type is not data_type:
                raise TypeError("price record type mismatch")
        elif data_type is DataType.FUNDING_RATE:
            if not isinstance(record, FundingRateRecord):
                raise TypeError("funding data must contain FundingRateRecord")
        elif data_type is DataType.OPEN_INTEREST_HISTORY:
            if not isinstance(record, OpenInterestRecord):
                raise TypeError("open-interest data must contain OpenInterestRecord")
            if Decimal(record.sum_open_interest) < 0 or Decimal(record.sum_open_interest_value) < 0:
                issues.append(
                    ValidationIssue(
                        code="negative_open_interest",
                        severity=Severity.CRITICAL,
                        message="open-interest values must be non-negative",
                        start=record.timestamp,
                    )
                )

        if not accepted:
            continue
        if interval_stream and expected_step is not None:
            if first_accepted is None and expected_start is not None:
                leading_delta = record.timestamp - expected_start
                leading_missing = int(leading_delta // expected_step)
                if leading_missing > 0:
                    gap_start = record.timestamp - leading_missing * expected_step
                    _append_gap_evidence(
                        gaps=gaps,
                        issues=issues,
                        start=gap_start,
                        end=record.timestamp,
                        timeframe=timeframe or "",
                        missing_intervals=leading_missing,
                        evidence=(
                            f"requested_start={expected_start.isoformat()}",
                            f"first_record={record.timestamp.isoformat()}",
                        ),
                        listing_date=listing_date,
                        delisting_date=delisting_date,
                        position="leading",
                    )
            if previous_accepted is not None:
                delta = record.timestamp - previous_accepted.timestamp
                if delta > expected_step:
                    missing = int(delta / expected_step) - 1
                    _append_gap_evidence(
                        gaps=gaps,
                        issues=issues,
                        start=previous_accepted.timestamp + expected_step,
                        end=record.timestamp,
                        timeframe=timeframe or "",
                        missing_intervals=missing,
                        evidence=(
                            previous_accepted.timestamp.isoformat(),
                            record.timestamp.isoformat(),
                        ),
                        listing_date=listing_date,
                        delisting_date=delisting_date,
                        position="internal",
                    )
                elif delta < expected_step:
                    issues.append(
                        ValidationIssue(
                            code="overlapping_interval",
                            severity=Severity.CRITICAL,
                            message=(
                                "record intervals overlap or advance by less than one timeframe"
                            ),
                            start=previous_accepted.timestamp,
                            end=record.timestamp,
                        )
                    )
        if first_accepted is None:
            first_accepted = record
        previous_accepted = record
        if accept is not None:
            accept(record)

    if interval_stream and expected_step is not None and first_accepted is None:
        if expected_start is not None and expected_end is not None:
            missing_intervals = int((expected_end - expected_start) // expected_step)
            if missing_intervals > 0:
                _append_gap_evidence(
                    gaps=gaps,
                    issues=issues,
                    start=expected_start,
                    end=expected_end,
                    timeframe=timeframe or "",
                    missing_intervals=missing_intervals,
                    evidence=("no accepted interval records",),
                    listing_date=listing_date,
                    delisting_date=delisting_date,
                    position="complete-range",
                )
                reason = classify_gap_reason(
                    expected_start,
                    expected_end,
                    listing_date=listing_date,
                    delisting_date=delisting_date,
                )
                if reason is GapReason.UNKNOWN:
                    issues.append(
                        ValidationIssue(
                            code="empty_interval_stream",
                            severity=Severity.ERROR,
                            message="interval stream contains no accepted records",
                            start=expected_start,
                            end=expected_end,
                        )
                    )
        else:
            issues.append(
                ValidationIssue(
                    code="empty_interval_stream",
                    severity=Severity.ERROR,
                    message="interval stream contains no accepted records",
                )
            )
    elif (
        interval_stream
        and expected_step is not None
        and previous_accepted is not None
        and expected_end is not None
    ):
        first_missing = previous_accepted.timestamp + expected_step
        trailing_delta = expected_end - first_missing
        trailing_missing = int(trailing_delta // expected_step)
        if trailing_missing > 0:
            gap_end = first_missing + trailing_missing * expected_step
            _append_gap_evidence(
                gaps=gaps,
                issues=issues,
                start=first_missing,
                end=gap_end,
                timeframe=timeframe or "",
                missing_intervals=trailing_missing,
                evidence=(
                    f"last_record={previous_accepted.timestamp.isoformat()}",
                    f"requested_end={expected_end.isoformat()}",
                ),
                listing_date=listing_date,
                delisting_date=delisting_date,
                position="trailing",
            )

    return DataValidationReport(
        status=_status(issues, duplicates),
        issues=tuple(issues),
        gaps=tuple(gaps),
        duplicates=tuple(duplicates),
        rows_checked=rows_checked,
        created_at=current_time,
    )


def validate_klines(
    records: Iterable[KlineRecord],
    *,
    instrument: InstrumentReference,
    timeframe: str,
    allow_provisional: bool = False,
    now: datetime | None = None,
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
    listing_date: datetime | None = None,
    delisting_date: datetime | None = None,
) -> tuple[tuple[KlineRecord, ...], DataValidationReport]:
    normalized, report = validate_records(
        records,
        data_type=DataType.KLINES,
        instrument=instrument,
        timeframe=timeframe,
        allow_provisional=allow_provisional,
        now=now,
        expected_start=expected_start,
        expected_end=expected_end,
        listing_date=listing_date,
        delisting_date=delisting_date,
    )
    return tuple(cast(KlineRecord, item) for item in normalized), report


def validate_records(
    records: Iterable[NormalizedRecord],
    *,
    data_type: DataType,
    instrument: InstrumentReference,
    timeframe: str | None,
    allow_provisional: bool = False,
    now: datetime | None = None,
    expected_start: datetime | None = None,
    expected_end: datetime | None = None,
    listing_date: datetime | None = None,
    delisting_date: datetime | None = None,
) -> tuple[tuple[NormalizedRecord, ...], DataValidationReport]:
    """Validate one isolated stream using the one-pass ordered validator."""

    ordered = sorted(records, key=lambda item: (item.timestamp, _record_identity(item)))
    clean: list[NormalizedRecord] = []
    report = validate_sorted_records(
        ordered,
        data_type=data_type,
        instrument=instrument,
        timeframe=timeframe,
        accept=clean.append,
        allow_provisional=allow_provisional,
        now=now,
        expected_start=expected_start,
        expected_end=expected_end,
        listing_date=listing_date,
        delisting_date=delisting_date,
    )
    return tuple(clean), report


def combine_validation_reports(
    reports: Iterable[DataValidationReport],
    *,
    created_at: datetime | None = None,
) -> DataValidationReport:
    items = tuple(reports)
    issues = tuple(issue for report in items for issue in report.issues)
    gaps = tuple(gap for report in items for gap in report.gaps)
    duplicates = tuple(duplicate for report in items for duplicate in report.duplicates)
    status = _status(issues, duplicates)
    return DataValidationReport(
        status=status,
        issues=issues,
        gaps=gaps,
        duplicates=duplicates,
        rows_checked=sum(report.rows_checked for report in items),
        created_at=(created_at or datetime.now(UTC)),
    )
