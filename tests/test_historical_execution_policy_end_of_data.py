from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_core.audit import Journal
from modular_core.config import EngineConfig, ExecutionConfig, RiskConfig
from modular_core.execution import BrokerSimulator
from modular_core.models import Candle, OrderRequest, OrderType, ReasonCode, Side


def _filled_broker(*, side: Side, current_close: bool) -> tuple[BrokerSimulator, Candle]:
    config = EngineConfig(
        execution=ExecutionConfig(
            slippage_bps=100.0,
            close_request_timing="current_close" if current_close else "next_open",
        )
    )
    broker = BrokerSimulator(config, Journal(run_id="historical-eod-test"))
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    request = OrderRequest(
        side=side,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
    )
    assert (
        broker.submit(
            request,
            source_module="canonical",
            created_at=created_at,
            current_index=0,
            mark_price=100.0,
        )
        is not None
    )
    bar = Candle(
        datetime(2026, 1, 1, 0, 5, tzinfo=UTC),
        100.0,
        106.0,
        94.0,
        102.0,
        1.0,
        "BTCUSDT",
        "5m",
    )
    broker.process_bar(bar, 1)
    assert broker.portfolio.position is not None
    return broker, bar


@pytest.mark.parametrize(
    ("side", "expected_multiplier"),
    ((Side.BUY, 0.99), (Side.SELL, 1.01)),
)
def test_current_close_mode_applies_adverse_market_pricing_at_end_of_data(
    side: Side,
    expected_multiplier: float,
) -> None:
    broker, bar = _filled_broker(side=side, current_close=True)

    trade = broker.close_position(
        timestamp=bar.timestamp,
        price=bar.close,
        reason=ReasonCode.END_OF_DATA.value,
        index=1,
        liquidity="taker",
        reference_price=bar.close,
    )

    assert trade is not None
    assert trade.exit_price == pytest.approx(bar.close * expected_multiplier)
    assert broker.journal.fills[-1].slippage == pytest.approx(
        abs(bar.close - bar.close * expected_multiplier)
    )


def test_default_end_of_data_close_pricing_remains_unchanged() -> None:
    broker, bar = _filled_broker(side=Side.BUY, current_close=False)

    trade = broker.close_position(
        timestamp=bar.timestamp,
        price=bar.close,
        reason=ReasonCode.END_OF_DATA.value,
        index=1,
        liquidity="taker",
        reference_price=bar.close,
    )

    assert trade is not None
    assert trade.exit_price == bar.close
    assert broker.journal.fills[-1].slippage == 0.0


def test_historical_limit_cost_requires_at_limit_fills() -> None:
    with pytest.raises(ValueError, match="requires execution.limit_fill_policy = at_limit"):
        EngineConfig(
            execution=ExecutionConfig(limit_fill_policy="price_improvement"),
            risk=RiskConfig(sizing_model="historical_limit_cost"),
        )
