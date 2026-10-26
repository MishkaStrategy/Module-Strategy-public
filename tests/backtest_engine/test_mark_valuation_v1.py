from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modular_backtest.funding import FundingEvent, FundingLineageEntry, RegisteredFundingSource
from modular_backtest.valuation import (
    MARK_VALUATION_MODEL_VERSION,
    FundingMarkAwareBrokerSimulator,
    MarkAwareBrokerSimulator,
    MarkValuationError,
    PriceLineageEntry,
    RegisteredMarkPriceSource,
    VALUATION_POLICY_LAST,
    VALUATION_POLICY_MARK,
    validate_valuation_inputs,
    valuation_application_metadata,
    valuation_application_run_id,
    valuation_runtime_evidence,
)
from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.instruments import InstrumentSpec
from modular_core.models import (
    Candle,
    OrderRequest,
    OrderType,
    Position,
    PositionSide,
    PositionUpdateRequest,
    Side,
)

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=5)
END = START + 2 * STEP


def _mark_source(
    *,
    opens: tuple[float, ...] = (120.0, 121.0),
    closes: tuple[float, ...] = (125.0, 126.0),
) -> RegisteredMarkPriceSource:
    return RegisteredMarkPriceSource(
        dataset_id="binance-btcusdt-mark",
        dataset_version=3,
        dataset_hash="b" * 64,
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
        timeframe="5m",
        window_start_inclusive=START,
        window_end_exclusive=START + STEP * len(opens),
        owner_row_count=len(opens),
        owner_source_record_count=len(opens),
        lineage=(
            PriceLineageEntry(
                artifact_id="mark-owner",
                source_kind="official_rest",
                checksum_sha256="a" * 64,
                record_count=len(opens),
                start=START,
                end=START + STEP * len(opens),
            ),
        ),
        mark_opens=opens,
        mark_closes=closes,
    )


def _bar(
    timestamp: datetime,
    *,
    open_price: float = 100.0,
    close_price: float = 101.0,
) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=open_price,
        high=max(open_price, close_price) + 1.0,
        low=min(open_price, close_price) - 1.0,
        close=close_price,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _position(*, entry_price: float = 100.0) -> Position:
    return Position(
        id="pos-1",
        entry_order_id="ord-1",
        side=PositionSide.LONG,
        quantity=1.0,
        entry_price=entry_price,
        entry_time=START,
        entry_index=0,
        entry_fee=0.0,
    )


def _broker(source: RegisteredMarkPriceSource) -> MarkAwareBrokerSimulator:
    return MarkAwareBrokerSimulator(
        EngineConfig(initial_cash=10_000),
        Journal(run_id="mark-v1"),
        InstrumentSpec(symbol="BTCUSDT"),
        mark_source=source,
    )


def _funding_source() -> RegisteredFundingSource:
    return RegisteredFundingSource(
        dataset_id="binance-btcusdt-funding",
        dataset_version=1,
        dataset_hash="c" * 64,
        symbol="BTCUSDT",
        coverage_start_inclusive=START,
        coverage_end_exclusive=END,
        event_count=1,
        source_record_count=1,
        lineage=(
            FundingLineageEntry(
                artifact_id="funding-owner",
                source_kind="official_rest",
                checksum_sha256="d" * 64,
                record_count=1,
                start=START,
                end=END,
            ),
        ),
        events=(
            FundingEvent(
                ordinal=0,
                timestamp=START,
                symbol="BTCUSDT",
                rate=Decimal("0.01"),
                mark_price=Decimal("100"),
            ),
        ),
    )


def test_mark_source_identity_lookup_and_candle_alignment_are_deterministic() -> None:
    source = _mark_source()
    first = _bar(START)
    last = _bar(START + STEP)

    source.assert_matches_candles(first, last, 2)

    assert source.mark_open(first) == 120.0
    assert source.mark_close(first) == 125.0
    assert source.mark_close_at(START + STEP) == 126.0
    assert source.identity()["data_type"] == "mark_price"
    assert source.identity()["coverage_semantics"] == "[start,end)"
    assert len(source.identity_sha256()) == 64
    assert "storage" not in str(source.identity()).lower()
    assert "uri" not in str(source.identity()).lower()


def test_mark_source_fails_closed_on_shift_gap_symbol_and_count() -> None:
    source = _mark_source()

    with pytest.raises(MarkValuationError, match="aligned"):
        source.mark_close_at(START + timedelta(minutes=1))
    with pytest.raises(MarkValuationError, match="symbol"):
        source.mark_close(
            Candle(
                timestamp=START,
                open=100.0,
                high=101.0,
                low=99.0,
                close=100.0,
                symbol="ETHUSDT",
                timeframe="5m",
            )
        )
    with pytest.raises(MarkValuationError, match="row count"):
        source.assert_matches_candles(_bar(START), _bar(START + STEP), 3)
    with pytest.raises(MarkValuationError, match="end"):
        source.assert_matches_candles(_bar(START), _bar(START), 2)


def test_mark_source_rejects_wrong_market_and_selected_rows_beyond_owner_evidence() -> None:
    source = _mark_source()

    with pytest.raises(MarkValuationError, match="Binance USDT-M"):
        replace(source, exchange_id="other")
    with pytest.raises(MarkValuationError, match="Binance USDT-M"):
        replace(source, market_type="spot")

    one_row_lineage = replace(source.lineage[0], record_count=1)
    with pytest.raises(MarkValuationError, match="exceeds owner dataset row evidence"):
        replace(
            source,
            owner_row_count=1,
            owner_source_record_count=1,
            lineage=(one_row_lineage,),
        )


def test_default_last_policy_preserves_existing_application_identity() -> None:
    assert validate_valuation_inputs(VALUATION_POLICY_LAST, None) == VALUATION_POLICY_LAST
    assert valuation_application_run_id("core-run", VALUATION_POLICY_LAST, None) == "core-run"
    metadata = valuation_application_metadata(VALUATION_POLICY_LAST, None)

    assert metadata["valuation_model"] == "traded-last-baseline"
    assert metadata["execution_price_source"] == "traded_ohlc"
    assert metadata["execution_risk_reference"] == "traded_open"
    assert metadata["post_bar_reference"] == "traded_close"

    with pytest.raises(MarkValuationError, match="must not receive"):
        validate_valuation_inputs(VALUATION_POLICY_LAST, _mark_source())
    with pytest.raises(MarkValuationError, match="requires"):
        validate_valuation_inputs(VALUATION_POLICY_MARK, None)


def test_mark_policy_changes_only_application_identity_and_publishes_owner_evidence() -> None:
    source = _mark_source()
    first = valuation_application_run_id("core-run", VALUATION_POLICY_MARK, source)
    second = valuation_application_run_id("core-run", VALUATION_POLICY_MARK, source)
    evidence = dict(valuation_runtime_evidence(first, VALUATION_POLICY_MARK, source))

    assert first == second
    assert first.startswith("core-run-mark-")
    assert evidence["valuation_model"] == MARK_VALUATION_MODEL_VERSION
    assert evidence["mark_source_identity_sha256"] == source.identity_sha256()
    assert evidence["execution_price_source"] == "traded_ohlc"
    assert evidence["execution_risk_reference"] == "mark_open"
    assert evidence["post_bar_reference"] == "mark_close"


def test_mark_broker_keeps_market_fill_on_traded_open_but_equity_on_mark_close() -> None:
    source = _mark_source(opens=(120.0, 121.0), closes=(125.0, 126.0))
    broker = _broker(source)
    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
        ),
        source_module="test",
        created_at=START,
        current_index=-1,
        mark_price=100.0,
    )
    assert order is not None

    broker.process_bar(_bar(START, open_price=100.0, close_price=101.0), 0)

    assert broker.portfolio.position is not None
    assert broker.portfolio.position.entry_price == pytest.approx(100.0)
    assert broker.portfolio.unrealized_pnl(101.0) == pytest.approx(25.0)
    assert broker.portfolio.equity(101.0) == pytest.approx(broker.portfolio.cash + 25.0)


def test_mark_close_is_used_for_order_bracket_and_position_update_reference() -> None:
    source = _mark_source(opens=(120.0, 121.0), closes=(125.0, 126.0))
    broker = _broker(source)

    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
            stop_loss=110.0,
            take_profit=130.0,
        ),
        source_module="test",
        created_at=START,
        current_index=-1,
        mark_price=100.0,
    )
    assert order is not None

    broker.portfolio.position = _position()
    updated = broker.update_position(
        PositionUpdateRequest(stop_loss=110.0, take_profit=130.0, reason="mark-reference"),
        timestamp=START,
        source_module="test",
        mark_price=100.0,
    )
    assert updated is True
    assert broker.portfolio.position.stop_loss == pytest.approx(110.0)
    assert broker.portfolio.position.take_profit == pytest.approx(130.0)


def test_funding_and_mark_composition_settles_funding_then_projects_mark_equity() -> None:
    mark_source = _mark_source(opens=(120.0, 121.0), closes=(125.0, 126.0))
    broker = FundingMarkAwareBrokerSimulator(
        EngineConfig(initial_cash=10_000),
        Journal(run_id="funding-mark-v1"),
        InstrumentSpec(symbol="BTCUSDT"),
        funding_source=_funding_source(),
        mark_source=mark_source,
    )
    broker.portfolio.position = _position(entry_price=100.0)

    broker.process_bar(_bar(START, open_price=100.0, close_price=101.0), 0)

    assert broker.funding_cursor == 1
    assert broker.portfolio.cash == pytest.approx(9_999.0)
    assert broker.portfolio.unrealized_pnl(101.0) == pytest.approx(25.0)
    assert broker.portfolio.equity(101.0) == pytest.approx(10_024.0)
    funding_events = [
        event for event in broker.journal.events if event.event_type == "portfolio.funding"
    ]
    assert len(funding_events) == 1
