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
from modular_core.broker_state import (
    BrokerStateSnapshot,
    capture_broker_state,
    restore_broker_state,
)
from modular_core.execution import BrokerSimulator
from modular_core.models import Order


START = datetime(2026, 1, 1, tzinfo=UTC)


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


def _broker(run_id: str) -> BrokerSimulator:
    return BrokerSimulator(_config(), Journal(run_id=run_id))


def _submit_entry(broker: BrokerSimulator, *, current_index: int) -> Order:
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
        created_at=START + timedelta(minutes=5 * current_index),
        current_index=current_index,
        mark_price=100.0,
    )
    assert order is not None
    return order


def _fill_bar(index: int) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=101.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _target_bar(index: int) -> Candle:
    return Candle(
        timestamp=START + timedelta(minutes=5 * index),
        open=100.0,
        high=123.0,
        low=99.0,
        close=122.5,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _complete_trade(broker: BrokerSimulator) -> None:
    _submit_entry(broker, current_index=0)
    broker.process_bar(_fill_bar(1), 1)
    broker.process_bar(_target_bar(2), 2)
    assert broker.portfolio.position is None


def test_historical_closed_equity_restart_preserves_next_submit_and_fill_exactly() -> None:
    continuous = _broker("historical-recovery-continuous")
    _complete_trade(continuous)

    snapshot = capture_broker_state(continuous)
    assert snapshot.historical_closed_equity is not None
    assert snapshot.historical_closed_equity.hex() == continuous._historical_closed_equity.hex()

    restarted = _broker("historical-recovery-restarted")
    restore_broker_state(restarted, snapshot)

    assert restarted._historical_closed_equity.hex() == continuous._historical_closed_equity.hex()
    assert restarted.portfolio.cash.hex() == continuous.portfolio.cash.hex()

    continuous_order = _submit_entry(continuous, current_index=2)
    restarted_order = _submit_entry(restarted, current_index=2)
    assert restarted_order.quantity.hex() == continuous_order.quantity.hex()

    next_bar = _fill_bar(3)
    continuous.process_bar(next_bar, 3)
    restarted.process_bar(next_bar, 3)
    assert restarted.journal.fills[-1].quantity.hex() == continuous.journal.fills[-1].quantity.hex()


def test_historical_closed_equity_restart_preserves_open_position_state() -> None:
    source = _broker("historical-recovery-open-source")
    _submit_entry(source, current_index=0)
    source.process_bar(_fill_bar(1), 1)
    assert source.portfolio.position is not None

    snapshot = capture_broker_state(source)
    restarted = _broker("historical-recovery-open-restarted")
    restore_broker_state(restarted, snapshot)

    assert restarted.portfolio.position is not None
    assert snapshot.historical_closed_equity is not None
    assert restarted._historical_closed_equity.hex() == snapshot.historical_closed_equity.hex()
    assert restarted._historical_closed_equity.hex() == source._historical_closed_equity.hex()


def test_legacy_historical_snapshot_is_only_inferred_before_first_fill() -> None:
    pristine = _broker("historical-recovery-legacy-pristine")
    pristine_state = dict(capture_broker_state(pristine).to_state())
    pristine_state.pop("historical_closed_equity")
    legacy_pristine = BrokerStateSnapshot.from_state(pristine_state)
    restored_pristine = _broker("historical-recovery-legacy-restored")
    restore_broker_state(restored_pristine, legacy_pristine)
    assert restored_pristine._historical_closed_equity == restored_pristine.config.initial_cash

    executed = _broker("historical-recovery-legacy-executed")
    _complete_trade(executed)
    executed_state = dict(capture_broker_state(executed).to_state())
    executed_state.pop("historical_closed_equity")
    legacy_executed = BrokerStateSnapshot.from_state(executed_state)

    with pytest.raises(ValueError, match="missing historical closed equity"):
        restore_broker_state(_broker("historical-recovery-legacy-fail"), legacy_executed)


def test_legacy_stop_distance_snapshot_after_fill_remains_compatible() -> None:
    config = EngineConfig()
    source = BrokerSimulator(config, Journal(run_id="stop-distance-legacy-source"))
    order = source.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1.0,
        ),
        source_module="compat",
        created_at=START,
        current_index=0,
        mark_price=100.0,
    )
    assert order is not None
    source.process_bar(_fill_bar(1), 1)
    assert source.portfolio.position is not None

    legacy_state = dict(capture_broker_state(source).to_state())
    legacy_state.pop("historical_closed_equity")
    legacy_snapshot = BrokerStateSnapshot.from_state(legacy_state)
    restarted = BrokerSimulator(config, Journal(run_id="stop-distance-legacy-restarted"))
    restore_broker_state(restarted, legacy_snapshot)

    assert restarted.portfolio.position is not None
    assert restarted._historical_closed_equity == config.initial_cash


def test_historical_closed_equity_snapshot_rejects_nonfinite_value() -> None:
    state = dict(capture_broker_state(_broker("historical-recovery-finite")).to_state())
    state["historical_closed_equity"] = float("inf")

    with pytest.raises(ValueError, match="finite"):
        BrokerStateSnapshot.from_state(state)
