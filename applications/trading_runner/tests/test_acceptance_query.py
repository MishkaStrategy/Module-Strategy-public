from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime

import pytest

from modular_trading_runner.acceptance_query import ReadOnlyTradingRuntimeAcceptanceQuery
from modular_trading_runner.config import TradingRunnerConfig
from modular_trading_runner.enums import HealthStatus, RuntimeState
from modular_trading_runner.lifecycle import LifecycleMachine
from modular_trading_runner.runtime import TradingRunner
from modular_trading_runner.supervision import RuntimeHealthRegistry, SupervisionPolicy


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


def _config() -> TradingRunnerConfig:
    return TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "runtime-stage1",
            "portfolio_id": "portfolio-paper",
            "strategy_id": "strategy-btc-paper",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "binance-usdtm",
            "instruments": ["BTCUSDT"],
            "market_data_sources": ["registered-owner"],
            "modules": [],
        }
    )


def _runner(*, critical_failure: bool = False) -> TradingRunner:
    clock = _Clock()
    health = RuntimeHealthRegistry(policy=SupervisionPolicy(), clock=clock)
    health.register("market-data", critical=True)
    if critical_failure:
        for _ in range(3):
            health.record_failure("market-data", error="failed", timestamp=clock.now())
    else:
        health.heartbeat("market-data", status=HealthStatus.HEALTHY, timestamp=clock.now())

    runner = object.__new__(TradingRunner)
    runner.config = _config()
    runner.health = health
    runner.lifecycle = LifecycleMachine()
    return runner


def test_snapshot_projects_only_bounded_authoritative_identity_and_health() -> None:
    runner = _runner()
    before_state = runner.state

    snapshot = ReadOnlyTradingRuntimeAcceptanceQuery(runner).snapshot()

    assert snapshot.runtime_id == "runtime-stage1"
    assert snapshot.portfolio_id == "portfolio-paper"
    assert snapshot.strategy_id == "strategy-btc-paper"
    assert snapshot.execution_mode == "paper"
    assert snapshot.base_timeframe == "5m"
    assert snapshot.configuration_hash == runner.config.canonical_hash
    assert snapshot.native_state == RuntimeState.CREATED.value
    assert snapshot.ready is True
    assert snapshot.live is True
    assert snapshot.component_count == 1
    assert snapshot.critical_failure_count == 0
    assert runner.state is before_state
    assert set(asdict(snapshot)) == {
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "execution_mode",
        "base_timeframe",
        "configuration_hash",
        "native_state",
        "ready",
        "live",
        "component_count",
        "critical_failure_count",
    }


def test_snapshot_reports_critical_health_without_exposing_details() -> None:
    snapshot = ReadOnlyTradingRuntimeAcceptanceQuery(_runner(critical_failure=True)).snapshot()

    assert snapshot.ready is False
    assert snapshot.live is False
    assert snapshot.component_count == 1
    assert snapshot.critical_failure_count == 1
    assert not hasattr(snapshot, "components")
    assert not hasattr(snapshot, "details")
    assert not hasattr(snapshot, "error")


def test_query_requires_authoritative_runtime_health_registry() -> None:
    runner = _runner()
    runner.health = object()

    with pytest.raises(TypeError, match="RuntimeHealthRegistry"):
        ReadOnlyTradingRuntimeAcceptanceQuery(runner)


def test_query_rejects_non_owner_objects() -> None:
    with pytest.raises(TypeError, match="runner must be TradingRunner"):
        ReadOnlyTradingRuntimeAcceptanceQuery(object())  # type: ignore[arg-type]
