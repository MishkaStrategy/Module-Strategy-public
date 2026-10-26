from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

modular_core = pytest.importorskip("modular_core")

from modular_trading_runner import RuntimeState, TradingRunner, TradingRunnerConfig  # noqa: E402
from modular_trading_runner.errors import (  # noqa: E402
    CriticalQueueOverflow,
    MarketDataRejected,
)
from modular_trading_runner.market_data import (  # noqa: E402
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventValidator,
)
from modular_trading_runner.pipeline import CoreMarketPipeline  # noqa: E402
from modular_trading_runner.service import TradingRuntimeService  # noqa: E402

Candle = modular_core.Candle
MarketView = modular_core.MarketView
ModuleDecision = modular_core.ModuleDecision
ModuleManifest = modular_core.ModuleManifest
ModuleRegistry = modular_core.ModuleRegistry
StrategyModule = modular_core.StrategyModule

NOW = datetime(2026, 7, 26, 12, 5, 2, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class Journal:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append(self, **kwargs: object) -> None:
        self.events.append(str(kwargs["event_type"]))


class Store:
    def load(self, key: str) -> None:
        return None

    def save(self, key: str, value: object) -> None:
        pass


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
        pass

    def stop(self) -> None:
        pass


class Executor:
    def accept(self, envelope: object, **kwargs: object) -> object:
        return envelope

    def cancel_pending_entries(self, **kwargs: object) -> bool:
        return True


class Checkpoint:
    def capture(self) -> None:
        pass


class NoopModule(StrategyModule):
    name = "noop"
    manifest = ModuleManifest(name=name)

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, view: object) -> object:
        self.calls += 1
        return ModuleDecision()


class FailOnceModule(StrategyModule):
    name = "fail-once"
    manifest = ModuleManifest(name=name)

    def __init__(self) -> None:
        self.calls = 0
        self.remaining_failures = 1

    def on_bar(self, view: object) -> object:
        self.calls += 1
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise RuntimeError("transient module failure")
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"calls": self.calls}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.calls = int(state["calls"])


class ViewFactory:
    def build(
        self,
        *,
        base_bars: tuple[object, ...],
        index: int,
        data_gap: bool,
        features: object,
    ) -> object:
        return MarketView(
            base_bars=base_bars,
            timeframe_bars={},
            index=index,
            cash=10000.0,
            equity=10000.0,
            position=None,
            pending_orders=(),
            data_gap=data_gap,
            features=features,
        )


def config(*, max_queue_size: int = 4) -> TradingRunnerConfig:
    return TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "runner-service",
            "portfolio_id": "portfolio-1",
            "strategy_id": "strategy-1",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "usdt_perpetual",
            "instruments": ("BTCUSDT",),
            "market_data_sources": ("binance-usdtm",),
            "modules": (),
            "max_queue_size": max_queue_size,
            "stale_after_seconds": 10.0,
            "max_future_skew_seconds": 1.0,
            "max_history_bars": 16,
            "max_tracked_market_events": 16,
        }
    )


def build_service(
    *,
    max_queue_size: int = 4,
    module: object | None = None,
) -> tuple[TradingRuntimeService, object]:
    runner_config = config(max_queue_size=max_queue_size)
    selected_module = module or NoopModule()
    runner = TradingRunner(
        config=runner_config,
        modules=(SimpleNamespace(on_start=lambda: None, on_finish=lambda: None),),
        planner=SimpleNamespace(
            validate_and_plan=lambda modules, base_timeframe: SimpleNamespace(modules=modules)
        ),
        clock=Clock(),
        journal=Journal(),
        state_store=Store(),
        recovery=Toggle(),
        reconciliation=Toggle(),
        health=Health(),
        market_data=MarketData(),
        plan_execution=Executor(),
        checkpoint=Checkpoint(),
    )
    plan = ModuleRegistry().validate_and_plan((selected_module,), base_timeframe="5m")
    pipeline = CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=ViewFactory(),
        max_history_bars=runner_config.max_history_bars,
    )
    policy = MarketDataPolicy.from_runner_config(runner_config)
    return (
        TradingRuntimeService(
            runner=runner,
            policy=policy,
            validator=MarketEventValidator(policy),
            pipeline=pipeline,
        ),
        selected_module,
    )


def event(
    *,
    event_id: str = "event-1",
    sequence: int = 1,
    timestamp: datetime = datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
    received_at: datetime = datetime(2026, 7, 26, 12, 5, 1, tzinfo=UTC),
) -> MarketEventEnvelope:
    candle = Candle(
        timestamp=timestamp,
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketEventEnvelope(
        event_id=event_id,
        source_id="binance-usdtm",
        market="usdt_perpetual",
        sequence=sequence,
        available_at=candle.close_time,
        received_at=received_at,
        candle=candle,
    )


def test_market_events_cannot_enter_before_running() -> None:
    service, _ = build_service()
    with pytest.raises(MarketDataRejected) as captured:
        service.enqueue_market_event(event())
    assert captured.value.reason_code == "runtime_not_running"


def test_accepted_event_runs_pipeline_only_after_running() -> None:
    service, module = build_service()
    service.runner.lifecycle.state = RuntimeState.RUNNING
    service.enqueue_market_event(event())
    result = service.process_next_market_event()
    assert result.batch is not None
    assert result.entry_allowed is True
    assert module.calls == 1


def test_duplicate_event_never_runs_modules_twice() -> None:
    service, module = build_service()
    service.runner.lifecycle.state = RuntimeState.RUNNING
    candidate = event()
    service.enqueue_market_event(candidate)
    service.process_next_market_event()
    service.enqueue_market_event(candidate)
    duplicate = service.process_next_market_event()
    assert duplicate.batch is None
    assert module.calls == 1


def test_stale_event_degrades_runtime_without_pipeline_side_effect() -> None:
    service, module = build_service()
    service.runner.lifecycle.state = RuntimeState.RUNNING
    service.enqueue_market_event(
        event(
            timestamp=datetime(2026, 7, 26, 11, 55, tzinfo=UTC),
            received_at=datetime(2026, 7, 26, 12, 0, 1, tzinfo=UTC),
        )
    )
    with pytest.raises(MarketDataRejected) as captured:
        service.process_next_market_event()
    assert captured.value.reason_code == "stale_event"
    assert service.runner.state is RuntimeState.DEGRADED
    assert module.calls == 0


def test_gap_degrades_runtime_and_blocks_entry_permission() -> None:
    service, module = build_service()
    service.runner.lifecycle.state = RuntimeState.RUNNING
    service.enqueue_market_event(event())
    service.process_next_market_event()
    service.enqueue_market_event(
        event(
            event_id="event-3",
            sequence=3,
            timestamp=datetime(2026, 7, 26, 12, 10, tzinfo=UTC),
            received_at=datetime(2026, 7, 26, 12, 15, 1, tzinfo=UTC),
        )
    )
    service.runner.clock.value = datetime(2026, 7, 26, 12, 15, 2, tzinfo=UTC)
    result = service.process_next_market_event()
    assert result.batch is not None
    assert result.batch.data_gap is True
    assert result.entry_allowed is False
    assert service.runner.state is RuntimeState.DEGRADED
    assert module.calls == 2


def test_degraded_runtime_still_accepts_market_data_for_protective_modules() -> None:
    service, _ = build_service()
    service.runner.lifecycle.state = RuntimeState.DEGRADED
    candidate = event()
    service.enqueue_market_event(candidate)
    assert service.queue.snapshot() == (candidate,)


def test_pipeline_failure_does_not_commit_market_identity_or_module_state() -> None:
    transient = FailOnceModule()
    service, module = build_service(module=transient)
    service.runner.lifecycle.state = RuntimeState.RUNNING
    candidate = event()
    service.enqueue_market_event(candidate)
    with pytest.raises(RuntimeError, match="transient module failure"):
        service.process_next_market_event()
    assert service.runner.state is RuntimeState.DEGRADED
    assert module.calls == 0

    service.enqueue_market_event(candidate)
    retried = service.process_next_market_event()
    assert retried.batch is not None
    assert module.calls == 1


def test_critical_overflow_degrades_runtime_without_dropping_queue() -> None:
    service, _ = build_service(max_queue_size=1)
    service.runner.lifecycle.state = RuntimeState.RUNNING
    first = event()
    second = event(
        event_id="event-2",
        sequence=2,
        timestamp=datetime(2026, 7, 26, 12, 5, tzinfo=UTC),
        received_at=datetime(2026, 7, 26, 12, 10, 1, tzinfo=UTC),
    )
    service.enqueue_market_event(first)
    with pytest.raises(CriticalQueueOverflow):
        service.enqueue_market_event(second)
    assert service.runner.state is RuntimeState.DEGRADED
    assert service.queue.snapshot() == (first,)
