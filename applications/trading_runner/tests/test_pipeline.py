from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest

modular_core = pytest.importorskip("modular_core")

from modular_trading_runner.errors import PipelineContractError  # noqa: E402
from modular_trading_runner.market_data import MarketEventEnvelope  # noqa: E402
from modular_trading_runner.pipeline import CoreMarketPipeline  # noqa: E402

Candle = modular_core.Candle
ClosePositionRequest = modular_core.ClosePositionRequest
Feature = modular_core.Feature
MarketView = modular_core.MarketView
ModuleDecision = modular_core.ModuleDecision
ModuleManifest = modular_core.ModuleManifest
ModuleRegistry = modular_core.ModuleRegistry
StrategyModule = modular_core.StrategyModule

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


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


class Producer(StrategyModule):
    name = "producer"
    manifest = ModuleManifest(name=name, produced_features=("producer.value",))

    def __init__(self) -> None:
        self.publish = True

    def on_bar(self, view: object) -> object:
        if not self.publish:
            return ModuleDecision()
        return ModuleDecision(
            features=(
                Feature(
                    key="producer.value",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value=view.current.close,
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                ),
            )
        )


class Consumer(StrategyModule):
    name = "consumer"
    manifest = ModuleManifest(name=name, required_features=("producer.value",))

    def on_bar(self, view: object) -> object:
        value = view.features.get("producer.value", symbol=view.current.symbol)
        return ModuleDecision(diagnostics={"observed": value})


class TransientConsumer(StrategyModule):
    name = "transient-consumer"
    manifest = ModuleManifest(name=name, required_features=("producer.value",))

    def __init__(self) -> None:
        self.calls = 0
        self.remaining_failures = 1

    def on_bar(self, view: object) -> object:
        self.calls += 1
        if self.remaining_failures:
            self.remaining_failures -= 1
            raise RuntimeError("transient consumer failure")
        value = view.features.get("producer.value", symbol=view.current.symbol)
        return ModuleDecision(diagnostics={"observed": value, "index": view.index})

    def snapshot_state(self) -> Mapping[str, object]:
        return {"calls": self.calls}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.calls = int(state["calls"])


class DirectExecutionModule(StrategyModule):
    name = "direct-execution"
    manifest = ModuleManifest(name=name)

    def on_bar(self, view: object) -> object:
        return ModuleDecision(closes=(ClosePositionRequest(),))


class FutureFeatureModule(StrategyModule):
    name = "future-feature"
    manifest = ModuleManifest(name=name, produced_features=("future.value",))

    def on_bar(self, view: object) -> object:
        return ModuleDecision(
            features=(
                Feature(
                    key="future.value",
                    source_module=self.name,
                    created_at=view.current.close_time + timedelta(seconds=1),
                    value=True,
                ),
            )
        )


def envelope(
    *,
    event_id: str = "event-1",
    sequence: int = 1,
    timestamp: datetime = NOW,
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
        received_at=candle.close_time + timedelta(seconds=1),
        candle=candle,
    )


def pipeline(*modules: object) -> CoreMarketPipeline:
    plan = ModuleRegistry().validate_and_plan(modules, base_timeframe="5m")
    return CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=ViewFactory(),
        max_history_bars=16,
    )


def test_feature_is_visible_to_later_module_on_same_candle() -> None:
    producer = Producer()
    consumer = Consumer()
    coordinator = pipeline(consumer, producer)
    batch = coordinator.process(envelope(), data_gap=False)
    assert batch.module_names == ("producer", "consumer")
    assert batch.decisions[1].diagnostics["observed"] == 101.0


def test_gap_invalidates_previous_features_before_module_execution() -> None:
    producer = Producer()
    consumer = Consumer()
    coordinator = pipeline(producer, consumer)
    coordinator.process(envelope(), data_gap=False)
    producer.publish = False
    second = coordinator.process(
        envelope(
            event_id="event-3",
            sequence=3,
            timestamp=NOW + timedelta(minutes=10),
        ),
        data_gap=True,
    )
    assert second.features_invalidated == 1
    assert second.decisions[1].diagnostics["observed"] is None


def test_pipeline_failure_rolls_back_feature_history_index_and_module_state() -> None:
    producer = Producer()
    consumer = TransientConsumer()
    coordinator = pipeline(consumer, producer)
    candidate = envelope()

    with pytest.raises(RuntimeError, match="transient consumer failure"):
        coordinator.process(candidate, data_gap=False)
    assert coordinator.feature_store.count == 0
    assert consumer.calls == 0

    retried = coordinator.process(candidate, data_gap=False)
    assert retried.decisions[1].diagnostics == {"observed": 101.0, "index": 0}
    assert coordinator.feature_store.count == 1
    assert consumer.calls == 1


def test_direct_execution_intent_is_rejected_before_side_effects() -> None:
    coordinator = pipeline(DirectExecutionModule())
    with pytest.raises(PipelineContractError) as captured:
        coordinator.process(envelope(), data_gap=False)
    assert captured.value.reason_code == "direct_execution_intent_forbidden"


def test_future_feature_is_rejected() -> None:
    coordinator = pipeline(FutureFeatureModule())
    with pytest.raises(PipelineContractError) as captured:
        coordinator.process(envelope(), data_gap=False)
    assert captured.value.reason_code == "future_feature"
