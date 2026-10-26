from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modular_backtest.funding import (
    FundingDataError,
    FundingEvent,
    FundingLineageEntry,
    FundingSettlementLedger,
    RegisteredFundingSource,
)
from modular_backtest.futures_execution import (
    FUNDING_RUNTIME_MODEL_VERSION,
    FundingAwareBrokerSimulator,
    FundingSettlementError,
    _decimal_from_float,
    _project_cash,
)
from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.instruments import InstrumentSpec
from modular_core.models import Candle, OrderRequest, OrderType, Position, PositionSide, Side


START = datetime(2026, 1, 1, tzinfo=UTC)
END = datetime(2026, 1, 2, tzinfo=UTC)


def _event(ordinal: int, timestamp: datetime, *, rate: str = "0.01") -> FundingEvent:
    return FundingEvent(
        ordinal=ordinal,
        timestamp=timestamp,
        symbol="BTCUSDT",
        rate=Decimal(rate),
        mark_price=Decimal("100"),
    )


def _source(*events: FundingEvent) -> RegisteredFundingSource:
    return RegisteredFundingSource(
        dataset_id="binance-btcusdt-funding",
        dataset_version=11,
        dataset_hash="c" * 64,
        symbol="BTCUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=len(events),
        source_record_count=len(events),
        lineage=(
            FundingLineageEntry(
                artifact_id="funding-owner",
                source_kind="official_rest",
                checksum_sha256="a" * 64,
                record_count=len(events),
                start=START,
                end=END,
            ),
        ),
        events=events,
    )


def _broker(
    source: RegisteredFundingSource,
    *,
    contract_size: float = 1.0,
) -> FundingAwareBrokerSimulator:
    return FundingAwareBrokerSimulator(
        EngineConfig(initial_cash=10_000),
        Journal(run_id="run-funding-runtime-v2"),
        InstrumentSpec(symbol="BTCUSDT", contract_size=contract_size),
        funding_source=source,
    )


def _position(side: PositionSide, *, quantity: float = 2.0) -> Position:
    return Position(
        id="pos-1",
        entry_order_id="ord-1",
        side=side,
        quantity=quantity,
        entry_price=90.0,
        entry_time=START,
        entry_index=0,
        entry_fee=0.0,
    )


def _bar(timestamp: datetime, *, price: float = 100.0) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=price,
        high=price,
        low=price,
        close=price,
        symbol="BTCUSDT",
        timeframe="5m",
    )


@pytest.mark.parametrize(
    ("side", "rate", "expected_cashflow"),
    [
        (PositionSide.LONG, "0.01", -1.0),
        (PositionSide.SHORT, "0.01", 1.0),
        (PositionSide.LONG, "-0.01", 1.0),
        (PositionSide.SHORT, "-0.01", -1.0),
    ],
)
def test_runtime_settles_decimal_funding_with_contract_size_before_bar(
    side: PositionSide,
    rate: str,
    expected_cashflow: float,
) -> None:
    event = _event(0, START, rate=rate)
    broker = _broker(_source(event), contract_size=0.5)
    broker.portfolio.position = _position(side)

    broker.process_bar(_bar(START), 0)

    assert broker.portfolio.cash == pytest.approx(10_000 + expected_cashflow)
    assert broker.funding_cursor == 1
    assert broker.cumulative_funding_cashflow == Decimal(str(expected_cashflow))
    funding_events = [
        item for item in broker.journal.events if item.event_type == "portfolio.funding"
    ]
    assert len(funding_events) == 1
    payload = funding_events[0].payload["funding"]
    assert payload["model_version"] == FUNDING_RUNTIME_MODEL_VERSION
    assert payload["position_side"] == side.value
    assert payload["position_quantity"] == "2.0"
    assert payload["contract_size"] == "0.5"
    assert payload["quantity"] == "1.00"
    assert payload["notional"] == "100.00"
    assert Decimal(payload["cashflow"]) == Decimal(str(expected_cashflow))


def test_flat_events_advance_exactly_once_without_prestart_timestamp_pollution() -> None:
    prestart = _event(0, START + timedelta(hours=1))
    boundary = _event(1, START + timedelta(hours=8))
    broker = _broker(_source(prestart, boundary))

    broker.process_bar(_bar(boundary.timestamp), 0)

    assert broker.funding_cursor == 2
    assert broker.cumulative_funding_cashflow == Decimal("0")
    funding_events = [
        item for item in broker.journal.events if item.event_type == "portfolio.funding"
    ]
    assert len(funding_events) == 1
    assert funding_events[0].timestamp == boundary.timestamp
    payload = funding_events[0].payload["funding"]
    assert payload["side"] == "flat"
    assert payload["cashflow"] == "0"


def test_funding_is_settled_before_order_becomes_eligible_on_same_bar() -> None:
    event = _event(0, START)
    broker = _broker(_source(event))
    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
        ),
        source_module="test",
        created_at=START - timedelta(minutes=5),
        current_index=-1,
        mark_price=100.0,
    )
    assert order is not None

    broker.process_bar(_bar(START), 0)

    funding_events = [
        item for item in broker.journal.events if item.event_type == "portfolio.funding"
    ]
    assert len(funding_events) == 1
    assert funding_events[0].payload["funding"]["side"] == "flat"
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.side is PositionSide.LONG


def test_runtime_fails_closed_when_funding_does_not_align_to_candle_open() -> None:
    event = _event(0, START + timedelta(minutes=1))
    broker = _broker(_source(event))

    broker.process_bar(_bar(START), 0)
    with pytest.raises(FundingSettlementError, match="align"):
        broker.process_bar(_bar(START + timedelta(minutes=5)), 1)


def test_funding_restart_state_restores_cursor_and_cash_exactly_once() -> None:
    first = _event(0, START)
    second = _event(1, START + timedelta(hours=8), rate="-0.02")
    source = _source(first, second)

    continuous = _broker(source)
    continuous.portfolio.position = _position(PositionSide.LONG, quantity=1.0)
    continuous.process_bar(_bar(first.timestamp), 0)
    continuous.process_bar(_bar(second.timestamp), 1)

    first_leg = _broker(source)
    first_leg.portfolio.position = _position(PositionSide.LONG, quantity=1.0)
    first_leg.process_bar(_bar(first.timestamp), 0)
    ledger_snapshot = dict(first_leg.funding_ledger.snapshot())

    restarted = _broker(source)
    restarted.portfolio.position = _position(PositionSide.LONG, quantity=1.0)
    restarted.portfolio.cash = first_leg.portfolio.cash
    restarted.funding_ledger = FundingSettlementLedger.restore(source, ledger_snapshot)
    restarted._funding_started = True
    restarted._funding_events_journaled = 1
    restarted.process_bar(_bar(second.timestamp), 1)

    assert restarted.funding_cursor == continuous.funding_cursor == 2
    assert restarted.cumulative_funding_cashflow == continuous.cumulative_funding_cashflow
    assert restarted.portfolio.cash == continuous.portfolio.cash
    assert restarted.funding_state() == continuous.funding_state()


def test_runtime_state_is_owner_bound_and_sanitized() -> None:
    source = _source(_event(0, START))
    broker = _broker(source)

    state = dict(broker.funding_state())

    assert state["model_version"] == FUNDING_RUNTIME_MODEL_VERSION
    assert state["source_identity_sha256"] == source.identity_sha256()
    assert state["source"]["dataset_hash"] == "c" * 64
    assert "storage" not in str(state).lower()
    assert "uri" not in str(state).lower()


def test_runtime_rejects_source_symbol_mismatch() -> None:
    event = FundingEvent(
        ordinal=0,
        timestamp=START,
        symbol="ETHUSDT",
        rate=Decimal("0.01"),
        mark_price=Decimal("100"),
    )
    source = RegisteredFundingSource(
        dataset_id="binance-ethusdt-funding",
        dataset_version=1,
        dataset_hash="d" * 64,
        symbol="ETHUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=1,
        source_record_count=1,
        lineage=(
            FundingLineageEntry(
                artifact_id="eth-owner",
                source_kind="official_rest",
                checksum_sha256="b" * 64,
                record_count=1,
                start=START,
                end=END,
            ),
        ),
        events=(event,),
    )

    with pytest.raises(FundingSettlementError, match="symbol"):
        _broker(source)


def test_runtime_rejects_processed_candle_outside_owner_coverage() -> None:
    broker = _broker(_source(_event(0, START)))

    with pytest.raises(FundingSettlementError, match="coverage"):
        broker.process_bar(_bar(END), 0)


def test_runtime_rejects_processed_candle_symbol_drift() -> None:
    broker = _broker(_source(_event(0, START)))
    wrong_symbol = Candle(
        timestamp=START,
        open=100.0,
        high=100.0,
        low=100.0,
        close=100.0,
        symbol="ETHUSDT",
        timeframe="5m",
    )

    with pytest.raises(FundingSettlementError, match="symbol"):
        broker.process_bar(wrong_symbol, 0)


def test_runtime_completeness_guard_and_future_event_branch() -> None:
    boundary = START + timedelta(hours=8)
    broker = _broker(_source(_event(0, boundary)))

    broker.process_bar(_bar(START), 0)
    broker.assert_funding_complete_through(START)
    with pytest.raises(FundingSettlementError, match="unsettled"):
        broker.assert_funding_complete_through(boundary)

    broker.process_bar(_bar(boundary), 1)
    broker.assert_funding_complete_through(boundary)
    broker.process_bar(_bar(boundary + timedelta(minutes=5)), 2)


def test_runtime_rejects_nonpositive_active_position_quantity() -> None:
    broker = _broker(_source(_event(0, START)))
    position = _position(PositionSide.LONG)
    position.quantity = 0.0
    broker.portfolio.position = position

    with pytest.raises(FundingSettlementError, match="quantity"):
        broker.process_bar(_bar(START), 0)


def test_runtime_wraps_owner_ledger_failure_and_rejects_unexpected_replay() -> None:
    source = _source(_event(0, START))
    broker = _broker(source)
    broker.portfolio.position = _position(PositionSide.LONG)

    class FailingLedger:
        next_ordinal = 0
        cumulative_cashflow = Decimal("0")

        @staticmethod
        def apply(*args: object, **kwargs: object) -> None:
            raise FundingDataError("owner ledger failed")

    broker.funding_ledger = FailingLedger()  # type: ignore[assignment]
    with pytest.raises(FundingSettlementError, match="owner ledger failed"):
        broker.process_bar(_bar(START), 0)

    class ReplayLedger:
        next_ordinal = 0
        cumulative_cashflow = Decimal("0")

        @staticmethod
        def apply(*args: object, **kwargs: object) -> None:
            return None

    broker = _broker(source)
    broker.portfolio.position = _position(PositionSide.LONG)
    broker.funding_ledger = ReplayLedger()  # type: ignore[assignment]
    with pytest.raises(FundingSettlementError, match="unexpectedly replayed"):
        broker.process_bar(_bar(START), 0)


def test_flat_consume_rejects_gap_out_of_range_and_owner_drift() -> None:
    source_event = _event(0, START)
    broker = _broker(_source(source_event))

    with pytest.raises(FundingSettlementError, match="gap"):
        broker._consume_flat(_event(1, START))

    class CursorTwoLedger:
        next_ordinal = 2
        cumulative_cashflow = Decimal("0")

    broker.funding_ledger = CursorTwoLedger()  # type: ignore[assignment]
    with pytest.raises(FundingSettlementError, match="exceeds"):
        broker._consume_flat(_event(2, START))

    broker = _broker(_source(source_event))
    with pytest.raises(FundingSettlementError, match="differs"):
        broker._consume_flat(_event(0, START, rate="0.02"))


@pytest.mark.parametrize("value", [True, "1", float("inf"), float("-inf"), float("nan")])
def test_decimal_projection_rejects_invalid_core_numbers(value: object) -> None:
    with pytest.raises(FundingSettlementError, match="finite number"):
        _decimal_from_float(value, "value")


def test_cash_projection_rejects_nonfinite_result() -> None:
    with pytest.raises(FundingSettlementError, match="not finite"):
        _project_cash(10_000.0, Decimal("1e10000"))
