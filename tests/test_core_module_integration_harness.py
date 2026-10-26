from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Mapping

from modular_core import (
    BacktestEngine,
    EngineConfig,
    Candle,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    SetupEvent,
    SetupStatus,
    Signal,
    Side,
    StrategyModule,
    capture_module_states,
    restore_module_states,
    verify_event_stream,
)


START = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _bars(count: int = 3) -> tuple[Candle, ...]:
    return tuple(
        Candle(
            timestamp=START + timedelta(minutes=5 * index),
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(count)
    )


class _Producer(StrategyModule):
    name = "producer"
    manifest = ModuleManifest(
        name=name,
        produced_features=("integration.feature",),
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(
            features=(
                Feature(
                    key="integration.feature",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value={"index": view.index},
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                ),
            )
        )


class _Consumer(StrategyModule):
    name = "consumer"
    manifest = ModuleManifest(
        name=name,
        required_features=("integration.feature",),
        skip_until_ready=False,
    )

    def __init__(self) -> None:
        self.observed: list[int] = []

    def on_bar(self, view: MarketView) -> ModuleDecision:
        assert view.features is not None
        value = view.features.get("integration.feature")
        assert isinstance(value, Mapping)
        index = value["index"]
        assert isinstance(index, int)
        self.observed.append(index)
        return ModuleDecision(
            signals=(
                Signal(
                    module=self.name,
                    side=Side.BUY,
                    strength=1.0,
                    reason=f"observed:{index}",
                ),
            )
        )


class _FailingProducer(StrategyModule):
    name = "failing"
    manifest = ModuleManifest(
        name=name,
        produced_features=("failed.feature",),
        skip_until_ready=False,
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        raise RuntimeError("expected integration failure")


class _Dependent(StrategyModule):
    name = "dependent"
    manifest = ModuleManifest(
        name=name,
        required_features=("failed.feature",),
        skip_until_ready=False,
    )

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision()


class _Independent(StrategyModule):
    name = "independent"
    manifest = ModuleManifest(name=name, skip_until_ready=False)

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision(
            setups=(
                SetupEvent(
                    module=self.name,
                    setup_id=f"setup-{view.index}",
                    status=SetupStatus.CREATED,
                    reason="integration",
                    symbol=view.current.symbol,
                ),
            )
        )


class _Stateful(StrategyModule):
    name = "stateful"
    manifest = ModuleManifest(name=name, skip_until_ready=False)

    def __init__(self) -> None:
        self.count = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.count += 1
        return ModuleDecision()

    def snapshot_state(self) -> Mapping[str, object]:
        return {"count": self.count}

    def restore_state(self, state: Mapping[str, object]) -> None:
        count = state.get("count")
        if not isinstance(count, int):
            raise TypeError("count must be int")
        self.count = count


def test_dependency_plan_propagates_feature_to_consumer_on_same_bar() -> None:
    producer = _Producer()
    consumer = _Consumer()

    result = BacktestEngine((consumer, producer)).run(_bars())

    assert result.modules == ("producer", "consumer")
    assert consumer.observed == [0, 1, 2]
    assert [item["signal"].reason for item in result.journal.signals] == [
        "observed:0",
        "observed:1",
        "observed:2",
    ]
    assert verify_event_stream(result.journal.events).valid


def test_runtime_failure_quarantines_dependents_but_not_independent_module() -> None:
    dependent = _Dependent()
    independent = _Independent()

    result = BacktestEngine(
        (_FailingProducer(), dependent, independent),
        EngineConfig(strict_module_errors=False),
    ).run(_bars())

    assert dependent.calls == 0
    assert independent.calls == 3
    reasons = [row["reason"] for row in result.journal.skips]
    assert any(reason.startswith("module_quarantined:on_bar:RuntimeError") for reason in reasons)
    assert any(reason == "module_quarantined:dependency:failing" for reason in reasons)
    assert verify_event_stream(result.journal.events).valid


def test_checkpoint_module_state_round_trip_is_deterministic() -> None:
    module = _Stateful()
    result = BacktestEngine(module).run(_bars(4))

    assert module.count == 4
    assert result.checkpoint.module_states == capture_module_states((module,))

    restored = _Stateful()
    restore_module_states((restored,), result.checkpoint.module_states)
    assert restored.count == 4
