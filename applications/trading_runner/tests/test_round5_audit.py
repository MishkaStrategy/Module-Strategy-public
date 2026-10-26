from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from modular_core import (
    Candle,
    InMemoryStateRepository,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    OrderEvent,
    StrategyModule,
    Trade,
)

from modular_trading_runner.cycle import (
    MarketCycleCoordinator,
    MarketCycleMetadata,
    MarketCycleStage,
    PaperBarExecutor,
    PaperBarRecoveryDecision,
)
from modular_trading_runner.errors import MarketCycleRecoveryRequired
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.market_data import (
    MarketDataPolicy,
    MarketEventEnvelope,
    MarketEventValidator,
)
from modular_trading_runner.pipeline import CoreMarketPipeline
from modular_trading_runner.routing import FactualEventEnvelope, FactualEventRouter

NOW = datetime(2026, 7, 26, 12, 5, 1, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


class FailingRepository:
    def __init__(self, fail_on: set[int]) -> None:
        self.delegate = InMemoryStateRepository()
        self.fail_on = set(fail_on)
        self.save_calls = 0

    def save(self, key: str, value: Mapping[str, object]) -> None:
        self.save_calls += 1
        if self.save_calls in self.fail_on:
            self.fail_on.remove(self.save_calls)
            raise RuntimeError("state repository unavailable")
        self.delegate.save(key, value)

    def load(self, key: str) -> Mapping[str, object] | None:
        return self.delegate.load(key)

    def delete(self, key: str) -> None:
        self.delegate.delete(key)


class Venue:
    def __init__(self) -> None:
        self.value = 0
        self.calls = 0

    def process_bar(self, bar: object, index: int) -> None:
        del bar
        self.calls += 1
        self.value += index + 1

    def snapshot_evidence(self, event: MarketEventEnvelope) -> Mapping[str, object]:
        del event
        return {"venue_state": self.value}


class Reconciliation:
    def resolve(
        self,
        *,
        event: MarketEventEnvelope,
        before: Mapping[str, object],
        current: Mapping[str, object],
    ) -> PaperBarRecoveryDecision:
        del event, before, current
        return PaperBarRecoveryDecision.APPLIED


def market_event() -> MarketEventEnvelope:
    candle = Candle(
        timestamp=datetime(2026, 7, 26, 12, 0, tzinfo=UTC),
        open=100.0,
        high=102.0,
        low=99.0,
        close=101.0,
        volume=10.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    return MarketEventEnvelope(
        event_id="market-1",
        source_id="binance-usdtm",
        market="usdt_perpetual",
        sequence=1,
        available_at=candle.close_time,
        received_at=candle.close_time + timedelta(seconds=1),
        candle=candle,
    )


def paper(repository: object, venue: Venue) -> PaperBarExecutor:
    return PaperBarExecutor(
        run_id="run-1",
        config_hash="a" * 64,
        state_repository=repository,
        venue=venue,
        evidence=venue,
        reconciliation=Reconciliation(),
    )


def test_paper_intent_save_failure_never_calls_venue() -> None:
    repository = FailingRepository({1})
    venue = Venue()
    executor = paper(repository, venue)
    with pytest.raises(RuntimeError, match="state repository"):
        executor.execute(market_event(), index=1)
    assert venue.calls == 0
    assert executor.recovery_required is False


def test_paper_commit_save_failure_restores_pending_for_reconciliation() -> None:
    repository = FailingRepository({2})
    venue = Venue()
    executor = paper(repository, venue)
    with pytest.raises(RuntimeError, match="state repository"):
        executor.execute(market_event(), index=1)
    assert venue.calls == 1
    assert executor.recovery_required is True

    recovered = paper(repository, venue).recover()
    assert recovered is not None
    assert recovered.recovered is True
    assert venue.calls == 1


def _advance_to_pipeline(metadata: MarketCycleMetadata) -> None:
    metadata.begin(market_event(), data_gap=False)
    metadata.transition(MarketCycleStage.RECEIVED, MarketCycleStage.PAPER_APPLIED)
    metadata.transition(MarketCycleStage.PAPER_APPLIED, MarketCycleStage.FACTUAL_APPLIED)
    metadata.transition(MarketCycleStage.FACTUAL_APPLIED, MarketCycleStage.PIPELINE_APPLIED)


def test_plan_validation_failure_does_not_advance_stage() -> None:
    metadata = MarketCycleMetadata()
    _advance_to_pipeline(metadata)
    invalid = SimpleNamespace(plan_id="plan-1", content_hash="invalid")
    with pytest.raises(ValueError, match="SHA-256"):
        metadata.transition(
            MarketCycleStage.PIPELINE_APPLIED,
            MarketCycleStage.PLAN_PREPARED,
            plan=invalid,
        )
    assert metadata.stage is MarketCycleStage.PIPELINE_APPLIED


def test_restore_rejects_incomplete_prepared_plan_identity() -> None:
    metadata = MarketCycleMetadata()
    _advance_to_pipeline(metadata)
    metadata.transition(
        MarketCycleStage.PIPELINE_APPLIED,
        MarketCycleStage.PLAN_PREPARED,
        plan=SimpleNamespace(plan_id="plan-1", content_hash="c" * 64),
    )
    state = metadata.snapshot_runtime_metadata()
    raw = state["market_cycle"]
    assert isinstance(raw, Mapping)
    tampered = dict(raw)
    tampered["plan_hash"] = None
    with pytest.raises(ValueError, match="complete plan identity"):
        MarketCycleMetadata().restore_runtime_metadata({"market_cycle": tampered})


class Module(StrategyModule):
    name = "round5-module"
    manifest = ModuleManifest(name=name)

    def __init__(self) -> None:
        self.calls = 0

    def on_bar(self, view: MarketView) -> ModuleDecision:
        self.calls += 1
        return ModuleDecision(diagnostics={"close": view.current.close})


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


class FactualSource:
    def __init__(self) -> None:
        self.reads = 0
        self.commits = 0

    def events_for(self, event: MarketEventEnvelope) -> Sequence[FactualEventEnvelope]:
        del event
        self.reads += 1
        return ()

    def commit(self, events: Sequence[FactualEventEnvelope]) -> None:
        assert tuple(events) == ()
        self.commits += 1


class Composer:
    def __init__(self, hashes: tuple[str, ...]) -> None:
        self.hashes = hashes
        self.calls = 0

    def compose(self, event: MarketEventEnvelope) -> object:
        del event
        digest = self.hashes[min(self.calls, len(self.hashes) - 1)]
        self.calls += 1
        return SimpleNamespace(plan_id="plan-1", content_hash=digest)


class Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, plan: object, event: MarketEventEnvelope) -> object:
        del plan, event
        self.calls += 1
        return {"accepted": True}


class Index:
    def index_for(self, event: MarketEventEnvelope) -> int:
        return event.sequence


class Checkpoint:
    def __init__(self, fail_on: set[int] | None = None) -> None:
        self.calls = 0
        self.fail_on = set() if fail_on is None else set(fail_on)

    def capture(self) -> object:
        self.calls += 1
        if self.calls in self.fail_on:
            self.fail_on.remove(self.calls)
            raise RuntimeError("checkpoint unavailable")
        return {"captured": self.calls}


def policy() -> MarketDataPolicy:
    return MarketDataPolicy(
        market="usdt_perpetual",
        instruments=("BTCUSDT",),
        sources=("binance-usdtm",),
        timeframes=("5m",),
        max_event_age_seconds=30.0,
        max_future_skew_seconds=1.0,
        max_tracked_events=32,
    )


def build_cycle(
    *,
    clock: Clock,
    checkpoint: Checkpoint,
    composer: Composer,
) -> tuple[MarketCycleCoordinator, Venue, FactualSource, Dispatcher, MarketCycleMetadata]:
    repository = InMemoryStateRepository()
    venue = Venue()
    module = Module()
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    pipeline = CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=ViewFactory(),
        max_history_bars=16,
    )
    journal = RuntimeEventJournal(run_id="run-1")
    router = FactualEventRouter(
        run_id="run-1",
        config_hash="a" * 64,
        module_graph_hash="b" * 64,
        consumer=Consumer(),
        state_repository=repository,
        journal=journal,
        clock=clock,
        queue_capacity=8,
    )
    source = FactualSource()
    dispatcher = Dispatcher()
    metadata = MarketCycleMetadata()
    coordinator = MarketCycleCoordinator(
        metadata=metadata,
        validator=MarketEventValidator(policy()),
        paper=paper(repository, venue),
        factual_source=source,
        factual_router=router,
        pipeline=pipeline,
        composer=composer,
        dispatcher=dispatcher,
        index=Index(),
        checkpoint=checkpoint,
        journal=journal,
        clock=clock,
    )
    return coordinator, venue, source, dispatcher, metadata


@pytest.mark.asyncio
async def test_recovery_uses_persisted_freshness_decision() -> None:
    clock = Clock()
    coordinator, venue, _, _, metadata = build_cycle(
        clock=clock,
        checkpoint=Checkpoint({2}),
        composer=Composer(("c" * 64,)),
    )
    with pytest.raises(MarketCycleRecoveryRequired, match="checkpoint"):
        await coordinator.process(market_event())
    assert metadata.stage is MarketCycleStage.PAPER_APPLIED
    clock.value += timedelta(minutes=10)
    result = await coordinator.process(market_event())
    assert result.plan_id == "plan-1"
    assert venue.calls == 1


@pytest.mark.asyncio
async def test_recomposed_plan_identity_change_blocks_before_dispatch() -> None:
    coordinator, _, _, dispatcher, metadata = build_cycle(
        clock=Clock(),
        checkpoint=Checkpoint(),
        composer=Composer(("c" * 64, "d" * 64)),
    )
    with pytest.raises(MarketCycleRecoveryRequired, match="identity differs"):
        await coordinator.process(market_event())
    assert metadata.stage is MarketCycleStage.PLAN_PREPARED
    assert dispatcher.calls == 0


@pytest.mark.asyncio
async def test_cycle_drains_factual_source_before_and_after_dispatch() -> None:
    coordinator, _, source, dispatcher, _ = build_cycle(
        clock=Clock(),
        checkpoint=Checkpoint(),
        composer=Composer(("c" * 64,)),
    )
    result = await coordinator.process(market_event())
    assert result.plan_id == "plan-1"
    assert dispatcher.calls == 1
    assert source.reads == 2
    assert source.commits == 2
