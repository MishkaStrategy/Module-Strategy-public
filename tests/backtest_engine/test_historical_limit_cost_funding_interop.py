from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from modular_backtest.funding import (
    FundingEvent,
    FundingLineageEntry,
    FundingSettlementLedger,
    RegisteredFundingSource,
)
from modular_backtest.futures_execution import FundingAwareBrokerSimulator, _project_cash
from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    FeeConfig,
    OrderRequest,
    OrderType,
    RiskConfig,
    Side,
)
from modular_core.audit import Journal
from modular_core.broker_state import capture_broker_state, restore_broker_state
from modular_core.instruments import InstrumentSpec


START = datetime(2026, 1, 1, tzinfo=UTC)
END = START + timedelta(days=1)


def _config() -> EngineConfig:
    return EngineConfig(
        fees=FeeConfig(maker_rate=0.0002, taker_rate=0.00055),
        execution=ExecutionConfig(stop_slippage_bps=2.5, target_liquidity="maker"),
        risk=RiskConfig(
            default_risk_fraction=0.005,
            max_risk_fraction=0.05,
            max_notional_multiple=100.0,
            sizing_model="historical_limit_cost",
        ),
    )


def _source(rate: str = "0.01") -> RegisteredFundingSource:
    event_time = START + timedelta(minutes=10)
    event = FundingEvent(
        ordinal=0,
        timestamp=event_time,
        symbol="BTCUSDT",
        rate=Decimal(rate),
        mark_price=Decimal("100"),
    )
    return RegisteredFundingSource(
        dataset_id="historical-funding-interop",
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
                checksum_sha256="a" * 64,
                record_count=1,
                start=START,
                end=END,
            ),
        ),
        events=(event,),
    )


def _broker(source: RegisteredFundingSource, run_id: str) -> FundingAwareBrokerSimulator:
    return FundingAwareBrokerSimulator(
        _config(),
        Journal(run_id=run_id),
        InstrumentSpec(symbol="BTCUSDT"),
        funding_source=source,
    )


def _direct_historical_quantity(
    *,
    equity: float,
    side: Side,
    stop: float,
) -> float:
    exit_side = Side.SELL if side is Side.BUY else Side.BUY
    stop_fill = stop * (1 + 2.5 / 10_000 * exit_side.sign)
    per_unit_risk = abs(100.0 - stop_fill) + 100.0 * 0.0002 + stop_fill * 0.00055
    return equity * 0.005 / per_unit_risk


def _bar(index: int, *, high: float, low: float, close: float) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=100.0,
        high=high,
        low=low,
        close=close,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


@pytest.mark.parametrize(
    ("side", "stop", "target", "target_high", "target_low"),
    (
        (Side.BUY, 95.0, 122.5, 123.0, 99.0),
        (Side.SELL, 105.0, 77.5, 101.0, 77.0),
    ),
)
def test_funding_survives_historical_full_close_and_recovery_changes_next_sizing(
    side: Side,
    stop: float,
    target: float,
    target_high: float,
    target_low: float,
) -> None:
    source = _source()
    broker = _broker(source, f"historical-funding-{side.value}")
    order = broker.submit(
        OrderRequest(
            side=side,
            order_type=OrderType.LIMIT,
            price=100.0,
            stop_loss=stop,
            take_profit=target,
            risk_fraction=0.005,
        ),
        source_module="canonical",
        created_at=START,
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None

    broker.process_bar(_bar(1, high=101.0, low=99.0, close=100.0), 1)
    assert broker.portfolio.position is not None
    closed_equity_before_funding = broker._historical_closed_equity

    broker.process_bar(
        _bar(2, high=target_high, low=target_low, close=target),
        2,
    )
    assert broker.portfolio.position is None
    funding_cashflow = broker.cumulative_funding_cashflow
    assert funding_cashflow != Decimal("0")
    if side is Side.BUY:
        assert funding_cashflow < 0
    else:
        assert funding_cashflow > 0

    trade = broker.journal.trades[-1]
    expected_after_funding = _project_cash(closed_equity_before_funding, funding_cashflow)
    expected_final_equity = expected_after_funding + trade.net_pnl
    no_funding_final_equity = closed_equity_before_funding + trade.net_pnl

    assert broker.portfolio.cash.hex() == expected_final_equity.hex()
    assert broker._historical_closed_equity.hex() == expected_final_equity.hex()
    assert broker.portfolio.cash.hex() != no_funding_final_equity.hex()

    core_snapshot = capture_broker_state(broker)
    funding_snapshot = dict(broker.funding_ledger.snapshot())
    restarted = _broker(source, f"historical-funding-restart-{side.value}")
    restore_broker_state(restarted, core_snapshot)
    restarted.funding_ledger = FundingSettlementLedger.restore(source, funding_snapshot)
    restarted._funding_started = True
    restarted._funding_events_journaled = 1

    assert restarted._historical_closed_equity.hex() == broker._historical_closed_equity.hex()
    assert restarted.portfolio.cash.hex() == broker.portfolio.cash.hex()
    assert restarted.cumulative_funding_cashflow == broker.cumulative_funding_cashflow

    next_request = OrderRequest(
        side=side,
        order_type=OrderType.LIMIT,
        price=100.0,
        stop_loss=stop,
        risk_fraction=0.005,
    )
    next_order = broker.submit(
        next_request,
        source_module="canonical",
        created_at=START + timedelta(minutes=10),
        current_index=2,
        mark_price=100.0,
    )
    restarted_order = restarted.submit(
        next_request,
        source_module="canonical",
        created_at=START + timedelta(minutes=10),
        current_index=2,
        mark_price=100.0,
    )
    assert next_order is not None
    assert restarted_order is not None

    expected_next_quantity = _direct_historical_quantity(
        equity=expected_final_equity,
        side=side,
        stop=stop,
    )
    no_funding_next_quantity = _direct_historical_quantity(
        equity=no_funding_final_equity,
        side=side,
        stop=stop,
    )
    assert next_order.quantity.hex() == expected_next_quantity.hex()
    assert restarted_order.quantity.hex() == next_order.quantity.hex()
    assert next_order.quantity.hex() != no_funding_next_quantity.hex()
