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
from modular_trading_runner.errors import (
    MarketCycleRecoveryRequired,
    PaperBarIdentityConflict,
    PaperBarRecoveryRequired,
)
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


class VenueState:
    def __init__(self) -> None:
        self.value = 0
        self.calls = 0
        self.raise_after_apply = False
        self.raise_without_apply = False

    def process_bar(self, bar: object, index: int) -> None:
        self.calls += 1
        if self.raise_without_apply:
            self.raise_without_apply = False
            raise RuntimeError("venue failed before mutation")
        self.value += index + 1
        if self.raise_after_apply:
            self.raise_after_apply = False
            raise RuntimeError("venue failed after mutation")

    def snapshot_evidence(self, event: MarketEventEnvelope) -> Mapping[str, object]:
        return {"venue_state": self.value}


class Reconciliation:
    def __init__(self, decision: PaperBarRecoveryDecision) -> None:
        self.decision = decision
        self.calls = 0

    def resolve(
        self,
        *,
        event: MarketEventEnvelope,
        before: Mapping[str, object],
        current: Mapping[str, object],
    ) -> PaperBarRecoveryDecision:
        self.calls += 1
        return self.decision


class Module(StrategyModule):
    name = "cycle-module"
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


class EmptyFactualSource:
    def events_for(self, event: MarketEventEnvelope) -> Sequence[FactualEventEnvelope]:
        return ()


class Composer:
    def __init__(self, plan: object | None) -> None:
        self.plan = plan
        self.calls = 0

    def compose(self, event: MarketEventEnvelope) -> object | None:
        self.calls += 1
        return self.plan


class Dispatcher:
    def __init__(self) -> None:
        self.calls = 0

    async def dispatch(self, plan: object, event: MarketEventEnvelope) -> object:
        self.calls += 1
        return {"accepted": True}


class Index:
    def index_for(self, event: MarketEventEnvelope) -> int:
        return event.sequence


class Checkpoint:
    def __init__(self, *, fail_on: set[int] | None = None) -> None:
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


def market_event(*, event_id: str = "market-1", sequence: int = 1) -> MarketEventEnvelope:
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
        event_id=event_id,
        source_id="binance-usdtm",
        market="usdt_perpetual",
        sequence=sequence,
        available_at=candle.close_time,
        received_at=candle.close_time + timedelta(seconds=1),
        candle=candle,
    )


def paper_executor(
    *,
    repository: InMemoryStateRepository,
    venue: VenueState,
    decision: PaperBarRecoveryDecision = PaperBarRecoveryDecision.APPLIED,
) -> PaperBarExecutor:
    return PaperBarExecutor(
        run_id="run-1",
        config_hash="a" * 64,
        state_repository=repository,
        venue=venue,
        evidence=venue,
        reconciliation=Reconciliation(decision),
    )


def pipeline(module: Module) -> CoreMarketPipeline:
    plan = ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")
    return CoreMarketPipeline(
        dependency_plan=plan,
        view_factory=ViewFactory(),
        max_history_bars=16,
    )


def factual_router(
    repository: InMemoryStateRepository, journal: RuntimeEventJournal
) -> FactualEventRouter:
    return FactualEventRouter(
        run_id="run-1",
        config_hash="a" * 64,
        module_graph_hash="b" * 64,
        consumer=Consumer(),
        state_repository=repository,
        journal=journal,
        clock=Clock(),
        queue_capacity=8,
    )


def cycle(
    *,
    repository: InMemoryStateRepository,
    venue: VenueState,
    module: Module,
    plan: object | None,
    checkpoint: Checkpoint | None = None,
) -> tuple[MarketCycleCoordinator, Dispatcher, MarketCycleMetadata, RuntimeEventJournal]:
    journal = RuntimeEventJournal(run_id="run-1")
    metadata = MarketCycleMetadata()
    dispatcher = Dispatcher()
    coordinator = MarketCycleCoordinator(
        metadata=metadata,
        validator=MarketEventValidator(policy()),
        paper=paper_executor(repository=repository, venue=venue),
        factual_source=EmptyFactualSource(),
        factual_router=factual_router(repository, journal),
        pipeline=pipeline(module),
        composer=Composer(plan),
        dispatcher=dispatcher,
        index=Index(),
        checkpoint=checkpoint or Checkpoint(),
        journal=journal,
        clock=Clock(),
    )
    return coordinator, dispatcher, metadata, journal


def test_paper_bar_executor_is_idempotent_and_rejects_conflicts() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    executor = paper_executor(repository=repository, venue=venue)
    original = market_event()
    first = executor.execute(original, index=1)
    duplicate = executor.execute(original, index=1)
    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert venue.calls == 1
    changed = MarketEventEnvelope(
        event_id=original.event_id,
        source_id=original.source_id,
        market=original.market,
        sequence=original.sequence,
        available_at=original.available_at,
        received_at=original.received_at,
        candle=Candle(
            timestamp=original.candle.timestamp,
            open=100.0,
            high=103.0,
            low=99.0,
            close=102.0,
            volume=10.0,
            symbol="BTCUSDT",
            timeframe="5m",
        ),
    )
    with pytest.raises(PaperBarIdentityConflict):
        executor.execute(changed, index=1)


def test_paper_bar_recovery_marks_mutated_ambiguous_window_as_applied() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    venue.raise_after_apply = True
    executor = paper_executor(repository=repository, venue=venue)
    with pytest.raises(RuntimeError, match="after mutation"):
        executor.execute(market_event(), index=1)
    assert executor.recovery_required is True
    recovered = paper_executor(
        repository=repository,
        venue=venue,
        decision=PaperBarRecoveryDecision.APPLIED,
    ).recover()
    assert recovered is not None
    assert recovered.recovered is True
    assert venue.calls == 1


def test_paper_bar_recovery_blocks_when_reconciliation_is_uncertain() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    venue.raise_after_apply = True
    executor = paper_executor(repository=repository, venue=venue)
    with pytest.raises(RuntimeError):
        executor.execute(market_event(), index=1)
    blocked = paper_executor(
        repository=repository,
        venue=venue,
        decision=PaperBarRecoveryDecision.BLOCKED,
    )
    with pytest.raises(PaperBarRecoveryRequired, match="blocking"):
        blocked.recover()
    assert venue.calls == 1


def test_paper_bar_failure_without_mutation_is_safe_to_retry() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    venue.raise_without_apply = True
    executor = paper_executor(repository=repository, venue=venue)
    with pytest.raises(RuntimeError, match="before mutation"):
        executor.execute(market_event(), index=1)
    assert executor.recovery_required is False
    result = executor.execute(market_event(), index=1)
    assert result.duplicate is False
    assert venue.calls == 2


@pytest.mark.asyncio
async def test_market_cycle_completes_no_plan_path_once() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    module = Module()
    coordinator, dispatcher, metadata, journal = cycle(
        repository=repository,
        venue=venue,
        module=module,
        plan=None,
    )
    result = await coordinator.process(market_event())
    assert result.no_plan is True
    assert result.plan_id is None
    assert venue.calls == 1
    assert module.calls == 1
    assert dispatcher.calls == 0
    assert metadata.stage is None
    assert len(journal.read("market_cycle.completed")) == 1
    duplicate = await coordinator.process(market_event())
    assert duplicate.duplicate is True
    assert venue.calls == 1
    assert module.calls == 1


@pytest.mark.asyncio
async def test_market_cycle_dispatches_plan_and_commits_identity() -> None:
    repository = InMemoryStateRepository()
    plan = SimpleNamespace(plan_id="plan-1", content_hash="c" * 64)
    coordinator, dispatcher, _, journal = cycle(
        repository=repository,
        venue=VenueState(),
        module=Module(),
        plan=plan,
    )
    result = await coordinator.process(market_event())
    assert result.plan_id == "plan-1"
    assert result.plan_hash == "c" * 64
    assert result.no_plan is False
    assert dispatcher.calls == 1
    completed = journal.read("market_cycle.completed")[0]
    assert completed.payload["plan_id"] == "plan-1"
    duplicate = await coordinator.process(market_event())
    assert duplicate.duplicate is True
    assert duplicate.plan_id == "plan-1"
    assert duplicate.plan_hash == "c" * 64
    assert duplicate.no_plan is False
    assert dispatcher.calls == 1


@pytest.mark.asyncio
async def test_checkpoint_failure_after_paper_does_not_repeat_venue_side_effect() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    checkpoint = Checkpoint(fail_on={2})
    coordinator, _, metadata, _ = cycle(
        repository=repository,
        venue=venue,
        module=Module(),
        plan=None,
        checkpoint=checkpoint,
    )
    with pytest.raises(MarketCycleRecoveryRequired, match="checkpoint"):
        await coordinator.process(market_event())
    assert metadata.stage is MarketCycleStage.PAPER_APPLIED
    result = await coordinator.process(market_event())
    assert result.no_plan is True
    assert venue.calls == 1


def test_market_cycle_metadata_roundtrip_preserves_active_stage() -> None:
    metadata = MarketCycleMetadata()
    metadata.begin(market_event())
    metadata.transition(MarketCycleStage.RECEIVED, MarketCycleStage.PAPER_APPLIED)
    state = metadata.snapshot_runtime_metadata()
    restored = MarketCycleMetadata()
    restored.restore_runtime_metadata(state)
    assert restored.stage is MarketCycleStage.PAPER_APPLIED
    assert restored.current_event() == market_event()


def test_paper_bar_corrupt_state_fails_closed() -> None:
    repository = InMemoryStateRepository()
    venue = VenueState()
    executor = paper_executor(repository=repository, venue=venue)
    executor.execute(market_event(), index=1)
    state = repository.load(executor._state_key)
    assert state is not None
    tampered = dict(state)
    tampered["processed"] = {"market-1": "f" * 64}
    repository.save(executor._state_key, tampered)
    with pytest.raises(PaperBarRecoveryRequired, match="integrity"):
        paper_executor(repository=repository, venue=venue)
