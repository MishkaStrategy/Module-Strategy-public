from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

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
from modular_core.execution import BrokerSimulator


def _broker(config: EngineConfig) -> BrokerSimulator:
    return BrokerSimulator(config, Journal(run_id="historical-limit-cost-bit-exact"))


def _direct_historical_quantity(
    *,
    equity: float,
    risk_fraction: float,
    entry: float,
    stop: float,
    side: Side,
    stop_slippage_bps: float,
    maker_rate: float,
    taker_rate: float,
) -> float:
    exit_side = Side.SELL if side is Side.BUY else Side.BUY
    stop_fill = stop * (1 + stop_slippage_bps / 10_000 * exit_side.sign)
    per_unit_risk = abs(entry - stop_fill) + entry * maker_rate + stop_fill * taker_rate
    return equity * risk_fraction / per_unit_risk


@pytest.mark.parametrize(
    ("side", "stop"),
    (
        (Side.BUY, 90.0),
        (Side.SELL, 110.0),
    ),
)
def test_historical_limit_cost_quantity_is_bit_exact_with_direct_expression(
    side: Side,
    stop: float,
) -> None:
    config = EngineConfig(
        fees=FeeConfig(maker_rate=0.0002, taker_rate=0.0005),
        execution=ExecutionConfig(stop_slippage_bps=10.0),
        risk=RiskConfig(
            default_risk_fraction=0.01,
            max_risk_fraction=0.05,
            max_notional_multiple=100.0,
            sizing_model="historical_limit_cost",
        ),
    )
    broker = _broker(config)
    order = broker.submit(
        OrderRequest(
            side=side,
            order_type=OrderType.LIMIT,
            price=100.0,
            stop_loss=stop,
            risk_fraction=0.01,
        ),
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )

    assert order is not None
    expected = _direct_historical_quantity(
        equity=10_000.0,
        risk_fraction=0.01,
        entry=100.0,
        stop=stop,
        side=side,
        stop_slippage_bps=10.0,
        maker_rate=0.0002,
        taker_rate=0.0005,
    )
    assert order.quantity == expected
    assert order.quantity.hex() == expected.hex()
    if side is Side.BUY:
        assert order.quantity.hex() == "0x1.3b1dfa752d29dp+3"


@pytest.mark.parametrize(
    ("side", "stop"),
    (
        (Side.BUY, 95.0),
        (Side.SELL, 105.0),
    ),
)
def test_canonical_historical_cost_constants_are_bit_exact(
    side: Side,
    stop: float,
) -> None:
    config = EngineConfig(
        fees=FeeConfig(maker_rate=0.0002, taker_rate=0.00055),
        execution=ExecutionConfig(stop_slippage_bps=2.5),
        risk=RiskConfig(
            default_risk_fraction=0.005,
            max_risk_fraction=0.05,
            max_notional_multiple=100.0,
            sizing_model="historical_limit_cost",
        ),
    )
    broker = _broker(config)
    order = broker.submit(
        OrderRequest(
            side=side,
            order_type=OrderType.LIMIT,
            price=100.0,
            stop_loss=stop,
            risk_fraction=0.005,
        ),
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )

    assert order is not None
    expected = _direct_historical_quantity(
        equity=10_000.0,
        risk_fraction=0.005,
        entry=100.0,
        stop=stop,
        side=side,
        stop_slippage_bps=2.5,
        maker_rate=0.0002,
        taker_rate=0.00055,
    )
    assert order.quantity == expected
    assert order.quantity.hex() == expected.hex()


def test_fill_time_resize_preserves_bit_exact_historical_quantity() -> None:
    config = EngineConfig(
        fees=FeeConfig(maker_rate=0.0002, taker_rate=0.0005),
        execution=ExecutionConfig(stop_slippage_bps=10.0),
        risk=RiskConfig(
            default_risk_fraction=0.01,
            max_risk_fraction=0.05,
            max_notional_multiple=100.0,
            sizing_model="historical_limit_cost",
        ),
    )
    broker = _broker(config)
    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            price=100.0,
            stop_loss=90.0,
            risk_fraction=0.01,
        ),
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None
    expected = _direct_historical_quantity(
        equity=10_000.0,
        risk_fraction=0.01,
        entry=100.0,
        stop=90.0,
        side=Side.BUY,
        stop_slippage_bps=10.0,
        maker_rate=0.0002,
        taker_rate=0.0005,
    )

    broker.process_bar(
        Candle(
            timestamp=datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
            open=101.0,
            high=102.0,
            low=99.0,
            close=100.5,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        ),
        1,
    )

    assert order.quantity == expected
    assert broker.journal.fills[-1].quantity == expected
    assert broker.journal.fills[-1].quantity.hex() == expected.hex()


def test_historical_limit_cost_reuses_exact_closed_equity_across_trades() -> None:
    config = EngineConfig(
        fees=FeeConfig(maker_rate=0.0002, taker_rate=0.00055),
        execution=ExecutionConfig(stop_slippage_bps=2.5, target_liquidity="maker"),
        risk=RiskConfig(
            default_risk_fraction=0.005,
            max_risk_fraction=0.05,
            max_notional_multiple=100.0,
            sizing_model="historical_limit_cost",
        ),
    )
    broker = _broker(config)
    start = datetime(2026, 1, 1, tzinfo=UTC)
    expected_equity = 10_000.0
    current_index = 0

    for _ in range(3):
        order = broker.submit(
            OrderRequest(
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                price=100.0,
                stop_loss=95.0,
                take_profit=122.5,
                risk_fraction=0.005,
            ),
            source_module="canonical",
            created_at=start + timedelta(minutes=5 * current_index),
            current_index=current_index,
            mark_price=100.0,
        )
        assert order is not None
        assert order.quantity == _direct_historical_quantity(
            equity=expected_equity,
            risk_fraction=0.005,
            entry=100.0,
            stop=95.0,
            side=Side.BUY,
            stop_slippage_bps=2.5,
            maker_rate=0.0002,
            taker_rate=0.00055,
        )

        broker.process_bar(
            Candle(
                timestamp=start + timedelta(minutes=5 * (current_index + 1)),
                open=101.0,
                high=101.0,
                low=99.0,
                close=100.0,
                volume=1.0,
                symbol="BTCUSDT",
                timeframe="5m",
            ),
            current_index + 1,
        )
        broker.process_bar(
            Candle(
                timestamp=start + timedelta(minutes=5 * (current_index + 2)),
                open=100.0,
                high=123.0,
                low=99.0,
                close=122.5,
                volume=1.0,
                symbol="BTCUSDT",
                timeframe="5m",
            ),
            current_index + 2,
        )
        expected_equity += broker.journal.trades[-1].net_pnl
        assert broker.portfolio.cash == expected_equity
        assert broker.portfolio.cash.hex() == expected_equity.hex()
        current_index += 2

    assert broker.portfolio.position is None

    next_order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            price=100.0,
            stop_loss=95.0,
            risk_fraction=0.005,
        ),
        source_module="canonical",
        created_at=start + timedelta(minutes=5 * current_index),
        current_index=current_index,
        mark_price=100.0,
    )
    assert next_order is not None
    expected_quantity = _direct_historical_quantity(
        equity=expected_equity,
        risk_fraction=0.005,
        entry=100.0,
        stop=95.0,
        side=Side.BUY,
        stop_slippage_bps=2.5,
        maker_rate=0.0002,
        taker_rate=0.00055,
    )
    assert next_order.quantity == expected_quantity
    assert next_order.quantity.hex() == expected_quantity.hex()

    broker.process_bar(
        Candle(
            timestamp=start + timedelta(minutes=5 * (current_index + 1)),
            open=101.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        ),
        current_index + 1,
    )
    assert broker.journal.fills[-1].quantity == expected_quantity
    assert broker.journal.fills[-1].quantity.hex() == expected_quantity.hex()
