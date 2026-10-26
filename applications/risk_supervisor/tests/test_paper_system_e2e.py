from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    Journal,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
)
from modular_core.execution import BrokerSimulator
from modular_strategy_trade_plan_router import (
    DecisionRouter,
    DownstreamPlanEnvelope,
    EntryCandidate,
    PortfolioSnapshot,
    ProtectionDecision,
    RouterInputPackage,
    SizingDecision,
    TargetIntent,
    TradeSide,
)
from modular_trading_runner import RuntimeState, TradingRunner, TradingRunnerConfig
from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PlanAction,
    RouterExecutionAdapter,
    RouterOCCSnapshot,
    StateVersions,
    VenueMetadataSnapshot,
)

from modular_risk_supervisor import (
    AuthoritativeRiskEvidence,
    RiskAdmissionBlocked,
    RiskGuardCode,
    RiskSupervisedPaperAdmission,
    RiskSupervisor,
    RiskSupervisorPolicy,
    SafetyState,
)

NOW = datetime(2026, 8, 28, 0, 0, tzinfo=UTC)
VERSIONS = StateVersions(portfolio=3, positions=4, orders=5)
OPERATOR_KEY = b"risk-supervisor-system-e2e-key-32-bytes"


class MemoryStore:
    def __init__(self) -> None:
        self.values: dict[str, dict[str, Any]] = {}

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.values[key] = dict(value)

    def load(self, key: str) -> Mapping[str, Any] | None:
        value = self.values.get(key)
        return dict(value) if value is not None else None


class StaticMetadataProvider:
    def __init__(self, metadata: VenueMetadataSnapshot) -> None:
        self.metadata = metadata

    def snapshot(self, instrument: str, timestamp: datetime) -> VenueMetadataSnapshot:
        del timestamp
        assert instrument == self.metadata.instrument
        return self.metadata


class OCCProvider:
    def __init__(self, envelope: DownstreamPlanEnvelope) -> None:
        self.values = {item.kind: item.expected for item in envelope.preconditions}
        self.calls = 0

    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterOCCSnapshot:
        self.calls += 1
        return RouterOCCSnapshot(
            portfolio_id=portfolio_id,
            instrument=instrument,
            observed_at=timestamp,
            values=self.values,
            source="risk-supervisor-system-e2e",
        )


class RunnerClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


class RunnerJournal:
    def __init__(self) -> None:
        self.events: list[str] = []

    def append(self, **kwargs: object) -> None:
        self.events.append(str(kwargs["event_type"]))


class RunnerPlanner:
    def validate_and_plan(
        self,
        modules: Sequence[Any],
        *,
        base_timeframe: str,
    ) -> object:
        assert base_timeframe == "5m"
        return SimpleNamespace(modules=tuple(modules))


class SuccessfulBoundary:
    def recover(self) -> bool:
        return True

    def reconcile(self) -> bool:
        return True


class ReadyHealth:
    def is_ready(self) -> bool:
        return True


class MarketDataBoundary:
    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class CheckpointBoundary:
    def capture(self) -> None:
        return None


def _metadata() -> VenueMetadataSnapshot:
    return VenueMetadataSnapshot(
        venue_id="paper-core",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="paper-metadata-v1",
        effective_from=NOW - timedelta(days=1),
        source_hash="a" * 64,
    )


def _paper_stack() -> tuple[BrokerSimulator, PaperExecutionCoordinator, VenueMetadataSnapshot]:
    metadata = _metadata()
    broker = BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=1.0),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )
    coordinator = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="risk-supervisor-system-e2e",
    )
    return broker, coordinator, metadata


def _evidence(
    snapshot_id: str,
    observed_at: datetime,
    *,
    transport_healthy: bool,
) -> AuthoritativeRiskEvidence:
    return AuthoritativeRiskEvidence(
        snapshot_id=snapshot_id,
        observed_at=observed_at,
        source_state_ids={
            "portfolio": f"portfolio-{snapshot_id}",
            "positions": f"positions-{snapshot_id}",
            "paper": f"paper-{snapshot_id}",
            "market": f"market-{snapshot_id}",
            "runner": f"runner-{snapshot_id}",
        },
        session_start_equity=Decimal("1000"),
        current_equity=Decimal("995"),
        peak_equity=Decimal("1000"),
        aggregate_exposure_notional=Decimal("1000"),
        market_data_observed_at=observed_at,
        consumer_progress_at=observed_at,
        reconciliation_blocked_scopes=0,
        reconciliation_loss_count=0,
        execution_attempts=20,
        execution_rejections=1,
        cancel_attempts=20,
        cancel_failures=1,
        fill_samples=20,
        mean_abs_slippage_bps=Decimal("5"),
        fill_quality_fraction=Decimal("0.95"),
        checkpoint_integrity_ok=True,
        disk_free_bytes=10_000_000_000,
        transport_healthy=transport_healthy,
        execution_latency_seconds=0.5,
    )


def _supervisor(*, transport_healthy: bool) -> RiskSupervisor:
    owner = RiskSupervisor(
        runtime_id="paper-runtime-e2e",
        policy=RiskSupervisorPolicy(),
        repository=MemoryStore(),
        operator_hmac_key=OPERATOR_KEY,
    )
    owner.evaluate(
        _evidence("snapshot-1", NOW, transport_healthy=transport_healthy),
        now=NOW,
    )
    return owner


def _router_open_envelope() -> DownstreamPlanEnvelope:
    entry = EntryCandidate(
        candidate_id="entry-a",
        source_decision_id="feature-entry-a",
        strategy_id="strategy-a",
        setup_id="setup-a",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-main",
        symbol="BTCUSDT",
        market="usdt_perpetual",
        side=TradeSide.LONG,
        signal_time=NOW,
        available_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_kind="risk-supervisor-e2e",
        order_type="limit",
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        trigger_price=None,
        confidence=Decimal("0.8"),
    )
    protection = ProtectionDecision(
        decision_id="protection-a",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        targets=(TargetIntent(price=Decimal("110"), allocation=Decimal("1")),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
    )
    sizing = SizingDecision(
        decision_id="sizing-a",
        request_id="request-a",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=True,
        quantity=Decimal("1"),
        notional=Decimal("100"),
        risk_amount=Decimal("5"),
        risk_fraction=Decimal("0.005"),
        leverage=Decimal("1"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )
    portfolio = PortfolioSnapshot(
        portfolio_id="portfolio-main",
        version="portfolio-v1",
        timestamp=NOW,
        equity=Decimal("10000"),
        available_balance=Decimal("9000"),
        used_margin=Decimal("1000"),
        free_margin=Decimal("9000"),
        risk_already_reserved=Decimal("0"),
        available_risk_budget=Decimal("300"),
        position_state_version="positions-v1",
        order_state_version="orders-v1",
        risk_reservation_version="risk-v1",
        positions=(),
    )
    result = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=portfolio,
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
        )
    )
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _runner(plan_execution: object) -> TradingRunner:
    config = TradingRunnerConfig.from_mapping(
        {
            "schema_version": "1",
            "runtime_id": "runner-e2e",
            "portfolio_id": "portfolio-main",
            "strategy_id": "strategy-a",
            "execution_mode": "paper",
            "base_timeframe": "5m",
            "market": "usdt_perpetual",
            "instruments": ("BTCUSDT",),
            "market_data_sources": ("paper-source",),
            "modules": (),
        }
    )
    return TradingRunner(
        config=config,
        modules=(),
        planner=RunnerPlanner(),
        clock=RunnerClock(NOW),
        journal=RunnerJournal(),
        state_store=MemoryStore(),
        recovery=SuccessfulBoundary(),
        reconciliation=SuccessfulBoundary(),
        health=ReadyHealth(),
        market_data=MarketDataBoundary(),
        plan_execution=plan_execution,
        checkpoint=CheckpointBoundary(),
    )


def _direct_plan(
    *,
    plan_id: str,
    action: PlanAction,
    intent: ExecutionIntent,
    timestamp: datetime,
    metadata: VenueMetadataSnapshot,
) -> ExecutableTradePlan:
    config = PaperExecutionConfig()
    return ExecutableTradePlan.create(
        plan_id=plan_id,
        created_at=timestamp,
        valid_from=timestamp,
        expires_at=timestamp + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="linear_futures",
        action=action,
        intents=(intent,),
        expected_versions=VERSIONS,
        metadata_version=metadata.metadata_version,
        configuration_hash=config.canonical_hash,
        input_hashes={"system-safety-e2e": "b" * 64},
    )


def _bar(timestamp: datetime) -> Candle:
    return Candle(
        timestamp=timestamp,
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


@pytest.mark.asyncio
async def test_halt_blocks_real_runner_router_paper_stack_before_any_execution_side_effect() -> (
    None
):
    owner = _supervisor(transport_healthy=False)
    broker, coordinator, _ = _paper_stack()
    envelope = _router_open_envelope()
    occ = OCCProvider(envelope)
    adapter = RouterExecutionAdapter(coordinator, occ_provider=occ)
    safety_boundary = RiskSupervisedPaperAdmission(
        supervisor=owner,
        runner_execution=adapter,
    )
    runner = _runner(safety_boundary)

    await runner.start()
    with pytest.raises(RiskAdmissionBlocked) as exc:
        await runner.dispatch_plan(
            envelope,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=VERSIONS,
        )

    assert RiskGuardCode.TRANSPORT_UNHEALTHY.value in exc.value.reason_codes
    assert runner.state is RuntimeState.RUNNING
    assert not runner.kill_switch.active
    assert occ.calls == 0
    assert coordinator.journal.store.read() == ()
    assert broker.orders == []


def test_halt_keeps_real_paper_reduce_path_available_for_preexisting_position() -> None:
    owner = _supervisor(transport_healthy=True)
    broker, coordinator, metadata = _paper_stack()
    safety_boundary = RiskSupervisedPaperAdmission(
        supervisor=owner,
        paper_admission=coordinator,
    )

    open_plan = _direct_plan(
        plan_id="paper-open-1",
        action=PlanAction.OPEN_POSITION,
        intent=ExecutionIntent(
            intent_id="entry-1",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
        ),
        timestamp=NOW,
        metadata=metadata,
    )
    opened = safety_boundary.accept_plan(
        open_plan,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        versions=VERSIONS,
    )
    assert opened.accepted
    assert len(opened.client_order_ids) == 1

    first_fill_time = NOW + timedelta(minutes=1)
    broker.process_bar(_bar(first_fill_time), 1)
    open_client_id = opened.client_order_ids[0]
    for event in tuple(broker.journal.orders):
        if event.order.client_order_id == open_client_id:
            coordinator.ingest_order_event(event)
    position = broker.portfolio.position_snapshot(100.0)
    assert position is not None
    assert position.side is PositionSide.LONG

    owner.evaluate(
        _evidence("snapshot-2", first_fill_time, transport_healthy=False),
        now=first_fill_time,
    )
    halted = owner.decision(now=first_fill_time)
    assert halted.state is SafetyState.HALT
    assert not halted.allow_risk_increase

    reduce_plan = _direct_plan(
        plan_id="paper-reduce-1",
        action=PlanAction.REDUCE_POSITION,
        intent=ExecutionIntent(
            intent_id="reduce-1",
            role=IntentRole.ENTRY,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal(str(position.quantity)),
            reduce_only=True,
            position_side=PositionSide.LONG,
        ),
        timestamp=first_fill_time,
        metadata=metadata,
    )
    reduced = safety_boundary.accept_plan(
        reduce_plan,
        now=first_fill_time,
        current_index=1,
        mark_price=Decimal("100"),
        versions=VERSIONS,
    )

    assert reduced.accepted
    assert len(broker.orders) == 2
    assert broker.orders[-1].reduce_only
    assert not broker.orders[-1].close_position
    assert owner.decision(now=first_fill_time).state is SafetyState.HALT

    broker.process_bar(_bar(NOW + timedelta(minutes=2)), 2)
    assert broker.portfolio.position is None
