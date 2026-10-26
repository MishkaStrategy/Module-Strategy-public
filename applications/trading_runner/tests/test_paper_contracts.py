from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from modular_core import InMemoryEventStore, OrderType, TimeInForce

paper_execution = pytest.importorskip("paper_execution")
router_contracts = pytest.importorskip("modular_strategy_trade_plan_router")

from modular_trading_runner.journal import RuntimeEventJournal  # noqa: E402
from modular_trading_runner.orchestration import (  # noqa: E402
    PaperScopedReconciliationCoordinator,
    ReconciliationScope,
)
from modular_trading_runner.ports import PlanExecutionBoundary  # noqa: E402

DecisionRouter = router_contracts.DecisionRouter
DownstreamPlanEnvelope = router_contracts.DownstreamPlanEnvelope
EntryCandidate = router_contracts.EntryCandidate
PortfolioSnapshot = router_contracts.PortfolioSnapshot
ProtectionDecision = router_contracts.ProtectionDecision
RouterInputPackage = router_contracts.RouterInputPackage
SizingDecision = router_contracts.SizingDecision
TargetIntent = router_contracts.TargetIntent
TradeSide = router_contracts.TradeSide

ExecutionEventJournal = paper_execution.ExecutionEventJournal
ExecutionResult = paper_execution.ExecutionResult
ReconciliationMode = paper_execution.ReconciliationMode
ReconciliationReport = paper_execution.ReconciliationReport
RouterExecutionAdapter = paper_execution.RouterExecutionAdapter
RouterOCCSnapshot = paper_execution.RouterOCCSnapshot
StateVersions = paper_execution.StateVersions
VenueMetadataSnapshot = paper_execution.VenueMetadataSnapshot

NOW = datetime(2026, 7, 23, 2, 0, tzinfo=UTC)


class Clock:
    def now(self) -> datetime:
        return NOW


class MetadataProvider:
    def snapshot(self, instrument: str, timestamp: datetime) -> VenueMetadataSnapshot:
        assert instrument == "BTCUSDT"
        return VenueMetadataSnapshot(
            venue_id="paper-binance-usdtm",
            instrument=instrument,
            market_type="usdt_perpetual",
            base_asset="BTC",
            quote_asset="USDT",
            tick_size=Decimal("0.1"),
            quantity_step=Decimal("0.001"),
            min_quantity=Decimal("0.001"),
            max_quantity=Decimal("1000"),
            min_notional=Decimal("5"),
            max_notional=Decimal("100000000"),
            contract_multiplier=Decimal("1"),
            permitted_order_types=(
                OrderType.LIMIT,
                OrderType.STOP_MARKET,
                OrderType.TAKE_PROFIT_LIMIT,
            ),
            permitted_time_in_force=(TimeInForce.GTC,),
            post_only_supported=True,
            reduce_only_supported=True,
            metadata_version="metadata-v1",
            effective_from=timestamp - timedelta(days=1),
            source_hash="d" * 64,
        )


class OCCProvider:
    def __init__(self, envelope: DownstreamPlanEnvelope) -> None:
        self.envelope = envelope

    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterOCCSnapshot:
        return RouterOCCSnapshot(
            portfolio_id=portfolio_id,
            instrument=instrument,
            observed_at=timestamp,
            values={item.kind: item.expected for item in self.envelope.preconditions},
            source="portfolio-state",
        )


class RecordingPaperCoordinator:
    def __init__(self) -> None:
        self.config = SimpleNamespace(canonical_hash="c" * 64)
        self.metadata_provider = MetadataProvider()
        self.journal = ExecutionEventJournal("paper-run", InMemoryEventStore())
        self._plan_results: dict[str, ExecutionResult] = {}
        self.accepted_plans: list[object] = []

    def accept_plan(
        self,
        plan: object,
        *,
        now: datetime,
        current_index: int,
        mark_price: Decimal,
        versions: StateVersions,
    ) -> ExecutionResult:
        assert current_index == 17
        assert mark_price == Decimal("100")
        assert isinstance(plan, paper_execution.ExecutableTradePlan)
        plan.validate(now, versions)
        self.accepted_plans.append(plan)
        result = ExecutionResult(
            plan_id=plan.plan_id,
            content_hash=plan.content_hash,
            accepted=True,
            terminal=False,
            client_order_ids=("paper-order-1",),
        )
        self._plan_results[plan.plan_id] = result
        return result


class ActualPaperReportBoundary:
    def __init__(self, report: ReconciliationReport) -> None:
        self.report = report
        self.calls: list[tuple[str, str, datetime | None]] = []

    def reconcile(
        self,
        *,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime | None = None,
    ) -> ReconciliationReport:
        self.calls.append((portfolio_id, instrument, timestamp))
        return self.report


class Checkpoint:
    def __init__(self) -> None:
        self.captures = 0

    def capture(self) -> object:
        self.captures += 1
        return {"captured": True}


def router_envelope() -> DownstreamPlanEnvelope:
    entry = EntryCandidate(
        candidate_id="entry-a",
        source_decision_id="feature-entry-a",
        strategy_id="strategy-a",
        setup_id="setup-a",
        module_id="entry_model",
        module_version="0.2.0",
        portfolio_id="portfolio-a",
        symbol="BTCUSDT",
        market="usdt_perpetual",
        side=TradeSide.LONG,
        signal_time=NOW,
        available_at=NOW,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_kind="limit_explicit",
        order_type="limit",
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        confidence=Decimal("0.8"),
    )
    protection = ProtectionDecision(
        decision_id="protection-entry-a",
        entry_candidate_id=entry.candidate_id,
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        side=entry.side,
        entry_price=Decimal("100"),
        stop_loss=Decimal("95"),
        targets=(TargetIntent(Decimal("110")),),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        risk_distance=Decimal("5"),
        risk_reward=Decimal("2"),
    )
    sizing = SizingDecision(
        decision_id="sizing-entry-a",
        request_id="request-entry-a",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=True,
        quantity=Decimal("10"),
        notional=Decimal("1000"),
        risk_amount=Decimal("50"),
        risk_fraction=Decimal("0.005"),
        leverage=Decimal("1"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )
    portfolio = PortfolioSnapshot(
        portfolio_id="portfolio-a",
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
    )
    routed = DecisionRouter().route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=portfolio,
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
        )
    )
    assert routed.plan is not None
    return DownstreamPlanEnvelope.from_plan(routed.plan)


def test_real_router_execution_adapter_satisfies_runner_boundary() -> None:
    envelope = router_envelope()
    coordinator = RecordingPaperCoordinator()
    adapter = RouterExecutionAdapter(
        coordinator,
        occ_provider=OCCProvider(envelope),
    )
    assert isinstance(adapter, PlanExecutionBoundary)
    versions = StateVersions(portfolio=1, positions=2, orders=3)
    result = adapter.accept(
        envelope,
        now=NOW,
        current_index=17,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )
    assert result.accepted is True
    assert result.plan_id == envelope.plan_id
    assert len(coordinator.accepted_plans) == 1
    plan = coordinator.accepted_plans[0]
    assert isinstance(plan, paper_execution.ExecutableTradePlan)
    assert plan.plan_id == envelope.plan_id
    assert plan.action.value == envelope.action.value
    assert plan.expected_versions == versions
    router_events = tuple(
        event
        for event in coordinator.journal.store.read()
        if event.event_type == "router.envelope_received"
    )
    assert len(router_events) == 1
    assert router_events[0].payload["content_hash"] == envelope.content_hash


def test_runner_reconciliation_accepts_actual_nonblocking_paper_report() -> None:
    report = ReconciliationReport(
        mode=ReconciliationMode.STRICT_BLOCK,
        findings=(),
    )
    paper = ActualPaperReportBoundary(report)
    checkpoint = Checkpoint()
    audit = RuntimeEventJournal(run_id="runner-run")
    coordinator = PaperScopedReconciliationCoordinator(
        paper=paper,
        scopes=(ReconciliationScope("portfolio-a", "BTCUSDT"),),
        journal=audit,
        clock=Clock(),
        checkpoint=checkpoint,
    )
    assert coordinator.reconcile() is True
    assert checkpoint.captures == 1
    assert len(audit.read("paper.reconciliation_completed")) == 1
    assert paper.calls == [("portfolio-a", "BTCUSDT", NOW)]
