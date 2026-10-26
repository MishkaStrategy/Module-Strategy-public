from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_backtest.valuation import (
    MarkAwareBrokerSimulator,
    PriceLineageEntry,
    RegisteredMarkPriceSource,
)
from modular_core.audit import Journal
from modular_core.config import EngineConfig, RiskConfig
from modular_core.instruments import InstrumentSpec
from modular_core.models import Candle, OrderRequest, OrderType, Position, PositionSide, Side

START = datetime(2026, 1, 1, tzinfo=UTC)
STEP = timedelta(minutes=5)


def _mark_source(
    *,
    opens: tuple[float, ...],
    closes: tuple[float, ...],
) -> RegisteredMarkPriceSource:
    end = START + STEP * len(opens)
    return RegisteredMarkPriceSource(
        dataset_id="binance-btcusdt-mark-risk-reference",
        dataset_version=1,
        dataset_hash="b" * 64,
        symbol="BTCUSDT",
        exchange_id="binance",
        market_type="usdt_m_perpetual",
        timeframe="5m",
        window_start_inclusive=START,
        window_end_exclusive=end,
        owner_row_count=len(opens),
        owner_source_record_count=len(opens),
        lineage=(
            PriceLineageEntry(
                artifact_id="mark-risk-reference-owner",
                source_kind="official_rest",
                checksum_sha256="a" * 64,
                record_count=len(opens),
                start=START,
                end=end,
            ),
        ),
        mark_opens=opens,
        mark_closes=closes,
    )


def _broker(
    source: RegisteredMarkPriceSource, *, allow_scale_in: bool = False
) -> MarkAwareBrokerSimulator:
    return MarkAwareBrokerSimulator(
        EngineConfig(
            initial_cash=10_000.0,
            allow_scale_in=allow_scale_in,
            risk=RiskConfig(
                default_risk_fraction=0.01,
                max_risk_fraction=0.05,
                max_notional_multiple=100.0,
            ),
        ),
        Journal(run_id="mark-risk-reference-v1"),
        InstrumentSpec(symbol="BTCUSDT"),
        mark_source=source,
    )


def _bar(timestamp: datetime, *, price: float) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=price,
        high=price + 1.0,
        low=price - 1.0,
        close=price,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def test_mark_close_drives_post_bar_risk_sizing_reference() -> None:
    source = _mark_source(opens=(120.0,), closes=(125.0,))
    broker = _broker(source)

    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            stop_loss=100.0,
        ),
        source_module="risk-reference",
        created_at=START,
        current_index=-1,
        mark_price=110.0,
    )

    assert order is not None
    assert order.quantity == pytest.approx(4.0)


def test_mark_open_drives_fill_time_equity_while_fill_stays_on_traded_open() -> None:
    source = _mark_source(
        opens=(105.0, 200.0),
        closes=(110.0, 210.0),
    )
    broker = _broker(source, allow_scale_in=True)
    broker.portfolio.position = Position(
        id="pos-existing",
        entry_order_id="ord-existing",
        side=PositionSide.LONG,
        quantity=100.0,
        entry_price=100.0,
        entry_time=START,
        entry_index=0,
        entry_fee=0.0,
    )

    broker.process_bar(_bar(START, price=105.0), 0)
    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            stop_loss=100.0,
        ),
        source_module="risk-reference",
        created_at=START,
        current_index=0,
        mark_price=105.0,
    )

    assert order is not None
    assert order.quantity == pytest.approx(11.0)

    broker.process_bar(_bar(START + STEP, price=150.0), 1)

    assert len(broker.journal.fills) == 1
    fill = broker.journal.fills[0]
    assert fill.price == pytest.approx(150.0)
    assert fill.quantity == pytest.approx(4.0)
