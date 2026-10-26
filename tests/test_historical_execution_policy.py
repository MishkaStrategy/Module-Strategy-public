from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from modular_core import (
    BrokerStateSnapshot,
    Candle,
    ClosePositionRequest,
    EngineConfig,
    ExecutionConfig,
    FeeConfig,
    OrderRequest,
    OrderType,
    RiskConfig,
    Side,
    capture_broker_state,
    restore_broker_state,
)
from modular_core.audit import Journal
from modular_core.execution import BrokerSimulator


def _bar(
    index: int,
    *,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> Candle:
    return Candle(
        datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5 * index),
        open_price,
        high,
        low,
        close,
        1.0,
        "BTCUSDT",
        "5m",
    )


def _broker(config: EngineConfig) -> BrokerSimulator:
    return BrokerSimulator(config, Journal(run_id="historical-policy-test"))


@pytest.mark.parametrize(
    ("side", "stop", "expected_stop_fill"),
    (
        (Side.BUY, 90.0, 89.91),
        (Side.SELL, 110.0, 110.11),
    ),
)
def test_historical_limit_cost_sizing_matches_fee_and_stop_slippage_denominator(
    side: Side,
    stop: float,
    expected_stop_fill: float,
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
    request = OrderRequest(
        side=side,
        order_type=OrderType.LIMIT,
        price=100.0,
        stop_loss=stop,
        risk_fraction=0.01,
    )

    order = broker.submit(
        request,
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )

    assert order is not None
    per_unit_risk = (
        abs(100.0 - expected_stop_fill)
        + 100.0 * config.fees.maker_rate
        + expected_stop_fill * config.fees.taker_rate
    )
    assert order.quantity == pytest.approx(10_000.0 * 0.01 / per_unit_risk)
    assert order.risk_fraction == 0.01


def test_historical_limit_cost_sizing_preserves_max_notional_cap() -> None:
    config = EngineConfig(
        execution=ExecutionConfig(stop_slippage_bps=10.0),
        risk=RiskConfig(
            default_risk_fraction=0.05,
            max_risk_fraction=0.05,
            max_notional_multiple=0.05,
            sizing_model="historical_limit_cost",
        ),
    )
    broker = _broker(config)

    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            price=100.0,
            stop_loss=99.0,
            risk_fraction=0.05,
        ),
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )

    assert order is not None
    assert order.quantity == pytest.approx(5.0)


def test_historical_limit_cost_rejects_non_limit_risk_order() -> None:
    config = EngineConfig(risk=RiskConfig(sizing_model="historical_limit_cost"))
    broker = _broker(config)

    order = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            stop_loss=95.0,
            risk_fraction=0.01,
        ),
        source_module="canonical",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        current_index=0,
        mark_price=100.0,
    )

    assert order is None
    assert broker.journal.skips[-1]["reason"] == "risk_rejected"


def test_limit_fill_bar_stop_is_opt_in_and_stop_only() -> None:
    default = _broker(EngineConfig(execution=ExecutionConfig(stop_slippage_bps=100.0)))
    historical = _broker(
        EngineConfig(
            execution=ExecutionConfig(
                stop_slippage_bps=100.0,
                allow_limit_stop_on_entry_bar=True,
            )
        )
    )
    request = OrderRequest(
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
    )
    created_at = datetime(2026, 1, 1, tzinfo=UTC)
    for broker in (default, historical):
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
        broker.process_bar(
            _bar(1, open_price=98.0, high=111.0, low=94.0, close=100.0),
            1,
        )

    assert default.portfolio.position is not None
    assert default.journal.trades == []
    assert historical.portfolio.position is None
    assert historical.journal.trades[0].exit_reason == "stop_on_fill_bar"
    assert historical.journal.trades[0].exit_price == pytest.approx(95.0 * 0.99)


def test_limit_fill_bar_target_alone_does_not_close_position() -> None:
    broker = _broker(EngineConfig(execution=ExecutionConfig(allow_limit_stop_on_entry_bar=True)))
    assert (
        broker.submit(
            OrderRequest(
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=1.0,
                price=100.0,
                stop_loss=95.0,
                take_profit=110.0,
            ),
            source_module="canonical",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            current_index=0,
            mark_price=100.0,
        )
        is not None
    )

    broker.process_bar(
        _bar(1, open_price=101.0, high=111.0, low=99.0, close=109.0),
        1,
    )

    assert broker.portfolio.position is not None
    assert broker.journal.trades == []


def test_short_limit_fill_bar_stop_uses_gap_open_and_adverse_stop_slippage() -> None:
    broker = _broker(
        EngineConfig(
            execution=ExecutionConfig(
                stop_slippage_bps=100.0,
                allow_limit_stop_on_entry_bar=True,
            )
        )
    )
    assert (
        broker.submit(
            OrderRequest(
                side=Side.SELL,
                order_type=OrderType.LIMIT,
                quantity=1.0,
                price=100.0,
                stop_loss=105.0,
            ),
            source_module="canonical",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            current_index=0,
            mark_price=100.0,
        )
        is not None
    )

    broker.process_bar(
        _bar(1, open_price=108.0, high=109.0, low=99.0, close=104.0),
        1,
    )

    assert broker.journal.trades[0].exit_reason == "stop_on_fill_bar"
    assert broker.journal.trades[0].exit_price == pytest.approx(108.0 * 1.01)


def test_current_close_request_uses_current_traded_close_with_adverse_slippage() -> None:
    broker = _broker(
        EngineConfig(
            execution=ExecutionConfig(
                slippage_bps=100.0,
                close_request_timing="current_close",
            )
        )
    )
    assert (
        broker.submit(
            OrderRequest(
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=1.0,
                price=100.0,
            ),
            source_module="canonical",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            current_index=0,
            mark_price=100.0,
        )
        is not None
    )
    current = _bar(1, open_price=100.0, high=103.0, low=99.0, close=102.0)
    broker.process_bar(current, 1)

    assert broker.schedule_close(
        ClosePositionRequest(reason="no_progress"),
        timestamp=current.timestamp,
        current_index=1,
        source_module="canonical",
    )

    assert broker.pending_close is None
    assert broker.portfolio.position is None
    trade = broker.journal.trades[0]
    assert trade.exit_reason == "no_progress"
    assert trade.exit_time == current.timestamp
    assert trade.exit_price == pytest.approx(102.0 * 0.99)
    exit_fill = broker.journal.fills[-1]
    assert exit_fill.liquidity == "taker"
    assert exit_fill.slippage == pytest.approx(102.0 - 102.0 * 0.99)


def test_default_close_request_timing_remains_next_open() -> None:
    broker = _broker(EngineConfig())
    assert (
        broker.submit(
            OrderRequest(
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=1.0,
                price=100.0,
            ),
            source_module="canonical",
            created_at=datetime(2026, 1, 1, tzinfo=UTC),
            current_index=0,
            mark_price=100.0,
        )
        is not None
    )
    current = _bar(1, open_price=100.0, high=103.0, low=99.0, close=102.0)
    broker.process_bar(current, 1)

    assert broker.schedule_close(
        ClosePositionRequest(reason="max_hold"),
        timestamp=current.timestamp,
        current_index=1,
        source_module="canonical",
    )
    assert broker.portfolio.position is not None
    assert broker.journal.trades == []

    next_bar = _bar(2, open_price=105.0, high=106.0, low=104.0, close=105.0)
    broker.process_bar(next_bar, 2)

    assert broker.journal.trades[0].exit_reason == "max_hold"
    assert broker.journal.trades[0].exit_time == next_bar.timestamp
    assert broker.journal.trades[0].exit_price == 105.0


def test_historical_policy_config_round_trips_broker_state() -> None:
    config = EngineConfig(
        execution=ExecutionConfig(
            allow_limit_stop_on_entry_bar=True,
            close_request_timing="current_close",
        ),
        risk=RiskConfig(sizing_model="historical_limit_cost"),
    )
    source = _broker(config)
    snapshot = capture_broker_state(source)
    restored = _broker(config)

    restore_broker_state(restored, snapshot)

    assert capture_broker_state(restored) == snapshot


def test_legacy_broker_state_defaults_new_policy_fields() -> None:
    config = EngineConfig()
    snapshot = capture_broker_state(_broker(config))
    state = dict(snapshot.to_state())
    config_state = dict(state["config"])
    execution_state = dict(config_state["execution"])
    risk_state = dict(config_state["risk"])
    execution_state.pop("allow_limit_stop_on_entry_bar")
    execution_state.pop("close_request_timing")
    risk_state.pop("sizing_model")
    config_state["execution"] = execution_state
    config_state["risk"] = risk_state
    state["config"] = config_state

    legacy_snapshot = BrokerStateSnapshot.from_state(state)
    restored = _broker(config)
    restore_broker_state(restored, legacy_snapshot)

    assert restored.config == config


def test_historical_policy_config_rejects_unknown_modes() -> None:
    with pytest.raises(ValueError, match="close_request_timing"):
        ExecutionConfig(close_request_timing="same_tick")
    with pytest.raises(ValueError, match="sizing_model"):
        RiskConfig(sizing_model="research_owned")
