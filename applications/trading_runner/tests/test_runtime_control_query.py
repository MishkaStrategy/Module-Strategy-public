from __future__ import annotations

from datetime import UTC, datetime

import pytest

from modular_trading_runner import RuntimeState, TradingRunner, TradingRunnerConfig
from modular_trading_runner.control_query import ReadOnlyTradingRuntimeQuery


class Clock:
    def now(self) -> datetime:
        return datetime(2026, 8, 7, 15, 30, tzinfo=UTC)


class Journal:
    def append(self, **kwargs: object) -> None:
        del kwargs


class Store:
    def __init__(self) -> None:
        self.data: dict[str, object] = {}

    def load(self, key: str) -> dict[str, object] | None:
        value = self.data.get(key)
        return value if isinstance(value, dict) else None

    def save(self, key: str, value: object) -> None:
        assert isinstance(value, dict)
        self.data[key] = value


class Planner:
    def validate_and_plan(self, modules: object, *, base_timeframe: str) -> object:
        class Plan:
            def __init__(self, rows: object) -> None:
                self.modules = tuple(rows)

        assert base_timeframe == "5m"
        return Plan(modules)


class Toggle:
    def recover(self) -> bool:
        return True

    def reconcile(self) -> bool:
        return True


class Health:
    def is_ready(self) -> bool:
        return True


class MarketData:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class Execution:
    def accept(self, envelope: object, **kwargs: object) -> object:
        del envelope, kwargs
        return {"accepted": True}

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        del kwargs
        return True


class Checkpoint:
    def capture(self) -> None:
        return None


def _config() -> TradingRunnerConfig:
    return TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "paper-runtime-1",
            "portfolio_id": "paper-portfolio-1",
            "strategy_id": "strategy-accepted-1",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "usdt_perpetual",
            "instruments": ("BTCUSDT",),
            "market_data_sources": ("binance-usdtm",),
            "modules": (),
        }
    )


def _runner() -> TradingRunner:
    return TradingRunner(
        config=_config(),
        modules=(),
        planner=Planner(),
        clock=Clock(),
        journal=Journal(),
        state_store=Store(),
        recovery=Toggle(),
        reconciliation=Toggle(),
        health=Health(),
        market_data=MarketData(),
        plan_execution=Execution(),
        checkpoint=Checkpoint(),
    )


@pytest.mark.asyncio
async def test_runtime_control_query_is_side_effect_free_and_omits_reason() -> None:
    runner = _runner()
    query = ReadOnlyTradingRuntimeQuery(runner)

    created = query.snapshot()
    assert created.native_state == RuntimeState.CREATED.value
    assert created.execution_mode == "paper"
    assert created.queue_depth is None

    await runner.start()
    await runner.activate_kill_switch(reason="private-operator-reason")
    before_state = runner.state
    before_kill_switch = runner.kill_switch

    snapshot = query.snapshot()

    assert snapshot.native_state == RuntimeState.KILL_SWITCH_ACTIVE.value
    assert snapshot.kill_switch_active is True
    assert snapshot.kill_switch_cancellation_pending is False
    assert snapshot.kill_switch_activated_at is not None
    assert "private-operator-reason" not in repr(snapshot)
    assert runner.state is before_state
    assert runner.kill_switch == before_kill_switch


def test_runtime_control_query_rejects_non_runner() -> None:
    with pytest.raises(TypeError, match="runner must be TradingRunner"):
        ReadOnlyTradingRuntimeQuery(object())  # type: ignore[arg-type]
