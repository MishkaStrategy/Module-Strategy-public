from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from modular_backtest.funding import (
    FundingDataError,
    FundingEvent,
    FundingLineageEntry,
    FundingSettlementLedger,
    RegisteredFundingSource,
    adapt_registered_funding_selection,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)


def _source() -> RegisteredFundingSource:
    lineage = (
        FundingLineageEntry(
            artifact_id="funding-a",
            source_kind="official_rest",
            checksum_sha256="a" * 64,
            record_count=1,
            start=START,
            end=START + timedelta(hours=12),
        ),
        FundingLineageEntry(
            artifact_id="funding-b",
            source_kind="official_rest",
            checksum_sha256="b" * 64,
            record_count=1,
            start=START + timedelta(hours=12),
            end=END,
        ),
    )
    events = (
        FundingEvent(
            ordinal=0,
            timestamp=START + timedelta(hours=8),
            symbol="BTCUSDT",
            rate=Decimal("0.0001"),
            mark_price=Decimal("90000"),
        ),
        FundingEvent(
            ordinal=1,
            timestamp=START + timedelta(hours=16),
            symbol="BTCUSDT",
            rate=Decimal("-0.0002"),
            mark_price=Decimal("91000"),
        ),
    )
    return RegisteredFundingSource(
        dataset_id="binance-btcusdt-funding",
        dataset_version=3,
        dataset_hash="c" * 64,
        symbol="BTCUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=2,
        source_record_count=2,
        lineage=lineage,
        events=events,
    )


def _scoped_source(
    *,
    scope_start: datetime | None = START + timedelta(hours=12),
    scope_end: datetime | None = END,
    events: tuple[FundingEvent, ...] | None = None,
    schema_version: int = 2,
) -> RegisteredFundingSource:
    owner = _source()
    if events is None:
        event = owner.events[1]
        events = (
            FundingEvent(
                ordinal=0,
                timestamp=event.timestamp,
                symbol=event.symbol,
                rate=event.rate,
                mark_price=event.mark_price,
            ),
        )
    return RegisteredFundingSource(
        dataset_id=owner.dataset_id,
        dataset_version=owner.dataset_version,
        dataset_hash=owner.dataset_hash,
        symbol=owner.symbol,
        coverage_start_inclusive=owner.coverage_start_inclusive,
        coverage_end_exclusive=owner.coverage_end_exclusive,
        event_count=len(events),
        source_record_count=owner.source_record_count,
        lineage=owner.lineage,
        events=events,
        schema_version=schema_version,
        scope_start_inclusive=scope_start,
        scope_end_exclusive=scope_end,
    )


def _selection(
    *, mark_price: Decimal | None = Decimal("90000")
) -> tuple[object, tuple[object, ...]]:
    source_artifact = SimpleNamespace(
        artifact_id="raw-funding",
        source=SimpleNamespace(value="official_rest"),
        checksum_sha256="d" * 64,
        record_count=2,
        start=START,
        end=END,
        source_uri="https://private.example/raw",
    )
    selection = SimpleNamespace(
        manifest=SimpleNamespace(instrument=SimpleNamespace(symbol="BTCUSDT")),
        source_artifacts=(source_artifact,),
        descriptor=SimpleNamespace(
            dataset_id="binance-btcusdt-funding",
            version=7,
            dataset_hash="e" * 64,
        ),
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=2,
        source_record_count=2,
    )
    records = (
        SimpleNamespace(
            funding_time=START + timedelta(hours=8),
            symbol="BTCUSDT",
            funding_rate=Decimal("0.0001"),
            mark_price=mark_price,
        ),
        SimpleNamespace(
            funding_time=START + timedelta(hours=16),
            symbol="BTCUSDT",
            funding_rate=Decimal("-0.0002"),
            mark_price=Decimal("91000"),
        ),
    )
    return selection, records


def test_adapter_binds_owner_dataset_lineage_and_counts_without_uris() -> None:
    selection, records = _selection()

    source = adapt_registered_funding_selection(
        selection,
        records,
        expected_symbol="BTCUSDT",
    )

    assert source.dataset_id == "binance-btcusdt-funding"
    assert source.dataset_version == 7
    assert source.dataset_hash == "e" * 64
    assert source.event_count == source.source_record_count == 2
    assert source.coverage_start_inclusive == START
    assert source.coverage_end_exclusive == END
    assert [event.ordinal for event in source.events] == [0, 1]
    identity = dict(source.identity())
    assert identity["coverage_semantics"] == "[start,end)"
    assert identity["lineage"][0]["checksum_sha256"] == "d" * 64
    assert "source_uri" not in str(identity)
    assert len(source.identity_sha256()) == 64


def test_adapter_fails_closed_when_owner_mark_price_is_missing() -> None:
    selection, records = _selection(mark_price=None)

    with pytest.raises(FundingDataError, match="missing owner mark_price"):
        adapt_registered_funding_selection(selection, records, expected_symbol="BTCUSDT")


def test_source_preserves_exclusive_end_coverage() -> None:
    source = _source()

    assert source.events_between(START, END) == source.events
    assert source.events_between(START, START + timedelta(hours=16)) == (source.events[0],)

    event_at_end = FundingEvent(
        ordinal=0,
        timestamp=END,
        symbol="BTCUSDT",
        rate=Decimal("0.0001"),
        mark_price=Decimal("90000"),
    )
    with pytest.raises(FundingDataError, match=r"outside \[start,end\)"):
        RegisteredFundingSource(
            dataset_id="binance-btcusdt-funding",
            dataset_version=1,
            dataset_hash="f" * 64,
            symbol="BTCUSDT",
            coverage_start_inclusive=START,
            coverage_end_exclusive=END,
            event_count=1,
            source_record_count=1,
            lineage=(
                FundingLineageEntry(
                    artifact_id="raw",
                    source_kind="official_rest",
                    checksum_sha256="1" * 64,
                    record_count=1,
                    start=START,
                    end=END,
                ),
            ),
            events=(event_at_end,),
        )


def test_scoped_source_preserves_owner_lineage_and_execution_identity() -> None:
    source = _scoped_source()
    scope_start = START + timedelta(hours=12)
    identity = dict(source.identity())

    assert source.event_count == 1
    assert source.source_record_count == 2
    assert identity["schema_version"] == 2
    assert identity["scope_start_inclusive"] == scope_start.isoformat()
    assert identity["scope_end_exclusive"] == END.isoformat()
    assert source.events_between(scope_start, END) == source.events
    with pytest.raises(FundingDataError, match="does not cover"):
        source.assert_covers(START, END)


@pytest.mark.parametrize(
    ("scope_start", "scope_end", "message"),
    (
        (None, END, "requires explicit scope bounds"),
        (START + timedelta(hours=12), None, "requires explicit scope bounds"),
        (START, START, "within owner coverage"),
        (START - timedelta(seconds=1), END, "within owner coverage"),
        (START, END + timedelta(seconds=1), "within owner coverage"),
    ),
)
def test_scoped_source_rejects_invalid_scope_bounds(
    scope_start: datetime | None,
    scope_end: datetime | None,
    message: str,
) -> None:
    with pytest.raises(FundingDataError, match=message):
        _scoped_source(scope_start=scope_start, scope_end=scope_end)


def test_scoped_source_rejects_event_outside_execution_scope() -> None:
    owner = _source()
    event = owner.events[0]
    outside = (
        FundingEvent(
            ordinal=0,
            timestamp=event.timestamp,
            symbol=event.symbol,
            rate=event.rate,
            mark_price=event.mark_price,
        ),
    )

    with pytest.raises(FundingDataError, match="outside scoped"):
        _scoped_source(events=outside)


def test_legacy_source_rejects_execution_scope() -> None:
    with pytest.raises(FundingDataError, match="legacy funding source"):
        _scoped_source(schema_version=1)


def test_settlement_cashflow_is_deterministic_for_long_and_short() -> None:
    source = _source()
    long_ledger = FundingSettlementLedger(source)
    short_ledger = FundingSettlementLedger(source)

    long = long_ledger.apply(source.events[0], side="long", quantity=Decimal("2"))
    short = short_ledger.apply(source.events[0], side="short", quantity=Decimal("2"))

    assert long is not None and short is not None
    assert long.notional == short.notional == Decimal("180000")
    assert long.cashflow == Decimal("-18.0000")
    assert short.cashflow == Decimal("18.0000")


def test_settlement_snapshot_replay_is_exactly_once() -> None:
    source = _source()
    continuous = FundingSettlementLedger(source)
    first = continuous.apply(source.events[0], side="long", quantity=Decimal("1"))
    assert first is not None
    snapshot = dict(continuous.snapshot())

    restarted = FundingSettlementLedger.restore(source, snapshot)
    assert restarted.apply(source.events[0], side="long", quantity=Decimal("1")) is None
    second = restarted.apply(source.events[1], side="long", quantity=Decimal("1"))
    assert second is not None

    uninterrupted = FundingSettlementLedger(source)
    assert uninterrupted.apply(source.events[0], side="long", quantity=Decimal("1")) is not None
    assert uninterrupted.apply(source.events[1], side="long", quantity=Decimal("1")) is not None

    assert restarted.next_ordinal == uninterrupted.next_ordinal == 2
    assert restarted.cumulative_cashflow == uninterrupted.cumulative_cashflow


def test_settlement_rejects_altered_replay() -> None:
    source = _source()
    ledger = FundingSettlementLedger(source)
    assert ledger.apply(source.events[0], side="long", quantity=Decimal("1")) is not None
    altered = FundingEvent(
        ordinal=0,
        timestamp=source.events[0].timestamp,
        symbol=source.events[0].symbol,
        rate=Decimal("0.0002"),
        mark_price=source.events[0].mark_price,
    )

    with pytest.raises(FundingDataError, match="replayed funding event differs"):
        ledger.apply(altered, side="long", quantity=Decimal("1"))


def test_settlement_rejects_gap_and_snapshot_from_another_source() -> None:
    source = _source()
    ledger = FundingSettlementLedger(source)

    with pytest.raises(FundingDataError, match="sequence has a gap"):
        ledger.apply(source.events[1], side="long", quantity=Decimal("1"))

    payload = dict(ledger.snapshot())
    other = RegisteredFundingSource(
        dataset_id=source.dataset_id,
        dataset_version=source.dataset_version,
        dataset_hash="9" * 64,
        symbol=source.symbol,
        coverage_start_inclusive=source.coverage_start_inclusive,
        coverage_end_exclusive=source.coverage_end_exclusive,
        event_count=source.event_count,
        source_record_count=source.source_record_count,
        lineage=source.lineage,
        events=source.events,
    )
    with pytest.raises(FundingDataError, match="another source"):
        FundingSettlementLedger.restore(other, payload)


def test_source_rejects_lineage_gap_and_count_drift() -> None:
    source = _source()
    gap_lineage = (
        source.lineage[0],
        FundingLineageEntry(
            artifact_id="funding-gap",
            source_kind="official_rest",
            checksum_sha256="8" * 64,
            record_count=1,
            start=START + timedelta(hours=13),
            end=END,
        ),
    )

    with pytest.raises(FundingDataError):
        RegisteredFundingSource(
            dataset_id=source.dataset_id,
            dataset_version=source.dataset_version,
            dataset_hash=source.dataset_hash,
            symbol=source.symbol,
            coverage_start_inclusive=source.coverage_start_inclusive,
            coverage_end_exclusive=source.coverage_end_exclusive,
            event_count=source.event_count,
            source_record_count=source.source_record_count,
            lineage=gap_lineage,
            events=source.events,
        )

    with pytest.raises(FundingDataError, match="source_record_count"):
        RegisteredFundingSource(
            dataset_id=source.dataset_id,
            dataset_version=source.dataset_version,
            dataset_hash=source.dataset_hash,
            symbol=source.symbol,
            coverage_start_inclusive=source.coverage_start_inclusive,
            coverage_end_exclusive=source.coverage_end_exclusive,
            event_count=source.event_count,
            source_record_count=3,
            lineage=source.lineage,
            events=source.events,
        )
