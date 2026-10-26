from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

import pytest
from modular_core import (
    Candle,
    Feature,
    InMemoryEventStore,
    InMemoryStateRepository,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    StrategyModule,
    TimeInForce,
    Trade,
)

from modular_trading_runner.checkpoint import RuntimeCheckpointCoordinator
from modular_trading_runner.errors import CheckpointIntegrityError
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.market_data import (
    BoundedCriticalQueue,
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventStatus,
    MarketEventValidator,
)
from modular_trading_runner.pipeline import CoreMarketPipeline
from modular_trading_runner.routing import (
    FactualEventEnvelope,
    FactualEventKind,
    FactualEventRouter,
)

NOW = datetime(2026, 7, 26, 12, 5, 2, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        return self.value


class StatefulModule(StrategyModule):
    name = "stateful"
    manifest = ModuleManifest(name=name, produced_features=("stateful.value",))

    def __init__(self) -> None:
        self.calls = 0
        self.fail_restore_once = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision(
            features=(
                Feature(
                    key="stateful.value",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value=self.calls,
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                ),
            )
        )

    def snapshot_state(self) -> Mapping[str, object]:
        return {"calls": self.calls}

    def restore_state(self, state: Mapping[str, object]) -> None:
        if self.fail_restore_once:
            self.fail_restore_once = False
            raise RuntimeError("module restore failed")
        self.calls = int(state["calls"])


class ViewFactory:
    def build(
        self,
        *,
        base_bars: tuple[Candle, ...],
        index: int,
        data_gap: bool,
        features: object,
    ) -> MarketView:
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


class Consumer:
    def on_order_event(self, event: OrderEvent) -> None:
        pass

    def on_trade(self, trade: Trade) -> None:
        pass

    def snapshot_state(self) -> Mapping[str, object]:
        return {}

    def restore_state(self, state: Mapping[str, object]) -> None:
        pass


class Metadata:
    def __init__(self) -> None:
        self.value: Mapping[str, object] = {
            "lifecycle": "running",
            "kill_switch": False,
        }
        self.fail_restore_once = False

    def snapshot_runtime_metadata(self) -> Mapping[str, object]:
        return self.value

    def restore_runtime_metadata(self, state: Mapping[str, object]) -> None:
        if self.fail_restore_once:
            self.fail_restore_once = False
            raise RuntimeError("metadata restore failed")
        self.value = dict(state)


def policy() -> MarketDataPolicy:
    return MarketDataPolicy(
        market="usdt_perpetual",
        instruments=("BTCUSDT",),
        sources=("binance-usdtm",),
        timeframes=("5m",),
        max_event_age_seconds=30.0,
        max_future_skew_seconds=1.0,
        max_tracked_events=16,
    )


def market_event(
    *,
    event_id: str = "market-1",
    sequence: int = 1,
    timestamp: datetime = datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
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


def factual_event() -> FactualEventEnvelope:
    order = OrderSnapshot(
        id="order-1",
        source_module="paper_execution",
        created_at=NOW,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id="paper-order-1",
        status=OrderStatus.FILLED,
        filled_quantity=1.0,
        remaining_quantity=0.0,
        average_fill_price=100.0,
        fees_paid=0.0,
        time_in_force=TimeInForce.GTC,
    )
    payload = OrderEvent(
        timestamp=NOW,
        event="filled",
        order=order,
        event_id="order-event-1",
    )
    return FactualEventEnvelope(
        event_id="factual-1",
        aggregate_id="portfolio-1:BTCUSDT",
        symbol="BTCUSDT",
        timestamp=payload.timestamp,
        kind=FactualEventKind.ORDER,
        payload=payload,
    )


def pipeline(module: StatefulModule) -> CoreMarketPipeline:
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    return CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=ViewFactory(),
        max_history_bars=16,
    )


def factual_router(
    *,
    repository: InMemoryStateRepository,
    journal: RuntimeEventJournal,
    clock: Clock,
) -> FactualEventRouter:
    return FactualEventRouter(
        run_id="run-1",
        config_hash="a" * 64,
        module_graph_hash="b" * 64,
        consumer=Consumer(),
        state_repository=repository,
        journal=journal,
        clock=clock,
        queue_capacity=4,
    )


def coordinator(
    *,
    repository: InMemoryStateRepository,
    journal: RuntimeEventJournal,
    validator: MarketEventValidator,
    queue: BoundedCriticalQueue[MarketEventEnvelope],
    market_pipeline: CoreMarketPipeline,
    router: FactualEventRouter,
    metadata: Metadata,
    clock: Clock,
    config_hash: str = "a" * 64,
) -> RuntimeCheckpointCoordinator:
    return RuntimeCheckpointCoordinator(
        run_id="run-1",
        config_hash=config_hash,
        module_graph_hash="b" * 64,
        repository=repository,
        clock=clock,
        journal=journal,
        market_validator=validator,
        market_queue=queue,
        pipeline=market_pipeline,
        factual_router=router,
        runtime_metadata=metadata,
    )


def prepared_checkpoint() -> tuple[
    InMemoryStateRepository,
    InMemoryEventStore,
    Clock,
    RuntimeCheckpointCoordinator,
]:
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    clock = Clock()
    journal = RuntimeEventJournal(run_id="run-1", store=event_store)
    journal.append(
        timestamp=clock.now(),
        event_type="runtime.started",
        aggregate_id="runner-1",
        payload={},
    )
    validator = MarketEventValidator(policy())
    accepted = market_event()
    validator.validate(accepted, now=NOW)
    queue: BoundedCriticalQueue[MarketEventEnvelope] = BoundedCriticalQueue(4)
    queue.put(
        market_event(
            event_id="market-2",
            sequence=2,
            timestamp=datetime(2026, 7, 26, 12, 5, tzinfo=UTC),
        )
    )
    module = StatefulModule()
    market_pipeline = pipeline(module)
    market_pipeline.process(accepted, data_gap=False)
    router = factual_router(repository=repository, journal=journal, clock=clock)
    router.enqueue(factual_event())
    checkpoint = coordinator(
        repository=repository,
        journal=journal,
        validator=validator,
        queue=queue,
        market_pipeline=market_pipeline,
        router=router,
        metadata=Metadata(),
        clock=clock,
    )
    checkpoint.capture()
    return repository, event_store, clock, checkpoint


def test_checkpoint_restores_market_pipeline_queues_and_metadata() -> None:
    repository, event_store, clock, _ = prepared_checkpoint()
    restored_validator = MarketEventValidator(policy())
    restored_queue: BoundedCriticalQueue[MarketEventEnvelope] = BoundedCriticalQueue(4)
    restored_module = StatefulModule()
    restored_pipeline = pipeline(restored_module)
    restored_journal = RuntimeEventJournal(run_id="run-1", store=event_store)
    restored_router = factual_router(
        repository=repository,
        journal=restored_journal,
        clock=clock,
    )
    restored_metadata = Metadata()
    restored_metadata.value = {"lifecycle": "created"}
    restored_checkpoint = coordinator(
        repository=repository,
        journal=restored_journal,
        validator=restored_validator,
        queue=restored_queue,
        market_pipeline=restored_pipeline,
        router=restored_router,
        metadata=restored_metadata,
        clock=clock,
    )
    assert restored_checkpoint.restore() is not None
    assert (
        restored_validator.validate(market_event(), now=NOW).status is MarketEventStatus.DUPLICATE
    )
    assert restored_queue.snapshot()[0].event_id == "market-2"
    assert restored_router.queue.snapshot() == (factual_event(),)
    assert restored_module.calls == 1
    assert restored_pipeline.feature_store.count == 1
    assert restored_metadata.value == {"kill_switch": False, "lifecycle": "running"}


def test_checkpoint_integrity_tampering_fails_before_restore() -> None:
    repository, _, _, checkpoint = prepared_checkpoint()
    snapshot = repository.load(checkpoint.state_key)
    assert snapshot is not None
    tampered = dict(snapshot)
    tampered["config_hash"] = "f" * 64
    repository.save(checkpoint.state_key, tampered)
    with pytest.raises(CheckpointIntegrityError, match="integrity hash"):
        checkpoint.restore()


def test_checkpoint_rejects_journal_divergence() -> None:
    _, _, clock, checkpoint = prepared_checkpoint()
    checkpoint.journal.append(
        timestamp=clock.now(),
        event_type="unexpected.external_append",
        aggregate_id="runner-1",
        payload={},
    )
    with pytest.raises(Exception, match="attestation"):
        checkpoint.restore()


def test_checkpoint_restores_after_clean_restart_journal_tail() -> None:
    _, _, clock, checkpoint = prepared_checkpoint()
    checkpoint.journal.append(
        timestamp=clock.now(),
        event_type="runtime.stopped",
        aggregate_id="runner-1",
        payload={},
    )
    checkpoint.journal.append(
        timestamp=clock.now(),
        event_type="runtime.configuring",
        aggregate_id="runner-1",
        payload={},
    )
    assert checkpoint.restore() is not None


def test_checkpoint_rejects_changed_configuration_identity() -> None:
    repository, event_store, clock, _ = prepared_checkpoint()
    journal = RuntimeEventJournal(run_id="run-1", store=event_store)
    incompatible = coordinator(
        repository=repository,
        journal=journal,
        validator=MarketEventValidator(policy()),
        queue=BoundedCriticalQueue(4),
        market_pipeline=pipeline(StatefulModule()),
        router=factual_router(repository=repository, journal=journal, clock=clock),
        metadata=Metadata(),
        clock=clock,
        config_hash="c" * 64,
    )
    with pytest.raises(CheckpointIntegrityError, match="identity"):
        incompatible.restore()


def test_failed_metadata_restore_rolls_back_pipeline_and_queues() -> None:
    repository, event_store, clock, _ = prepared_checkpoint()
    journal = RuntimeEventJournal(run_id="run-1", store=event_store)
    validator = MarketEventValidator(policy())
    queue: BoundedCriticalQueue[MarketEventEnvelope] = BoundedCriticalQueue(4)
    live_event = market_event(event_id="live-9", sequence=9)
    queue.put(live_event)
    module = StatefulModule()
    module.calls = 7
    metadata = Metadata()
    metadata.value = {"lifecycle": "before-restore"}
    metadata.fail_restore_once = True
    restored = coordinator(
        repository=repository,
        journal=journal,
        validator=validator,
        queue=queue,
        market_pipeline=pipeline(module),
        router=factual_router(repository=repository, journal=journal, clock=clock),
        metadata=metadata,
        clock=clock,
    )
    with pytest.raises(CheckpointIntegrityError, match="restore failed"):
        restored.restore()
    assert module.calls == 7
    assert queue.snapshot() == (live_event,)
    assert metadata.value == {"lifecycle": "before-restore"}
