from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    Journal,
    PositionSide,
)
from modular_core.execution import BrokerSimulator
from modular_core.models import Position
from modular_strategy_trade_plan_router import (
    DecisionRouter,
    DownstreamPlanEnvelope,
    EntryCandidate,
    ExistingPositionPolicy,
    PortfolioSnapshot,
    PositionState,
    ProtectionDecision,
    RouterConfiguration,
    RouterInputPackage,
    SizingDecision,
    TargetIntent,
    TradeSide,
)

from paper_execution import (
    DeferredProtectionLeg,
    ExecutionOrchestrationState,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperOrderState,
    PaperReasonCode,
    PlanValidationError,
    ProtectionActivationPolicy,
    ProtectionCoordinator,
    ReconciliationBlocked,
    RouterExecutionAdapter,
    RouterOCCSnapshot,
    RouterPositionSnapshot,
    StateVersions,
    VenueMarkSnapshot,
)

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
BASELINE_QUANTITY = Decimal("5")
ADD_QUANTITY = Decimal("10")


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


class StaticOCCProvider:
    def __init__(self, envelope: DownstreamPlanEnvelope) -> None:
        self.values = {item.kind: item.expected for item in envelope.preconditions}

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
            values=self.values,
            source="router-add-test",
        )


class StaticPositionProvider:
    def __init__(
        self,
        *,
        quantity: Decimal = BASELINE_QUANTITY,
        position_id: str = "position-a",
        side: PositionSide = PositionSide.LONG,
    ) -> None:
        self.quantity = quantity
        self.position_id = position_id
        self.side = side
        self.calls = 0

    def snapshot(
        self,
        portfolio_id: str,
        instrument: str,
        timestamp: datetime,
    ) -> RouterPositionSnapshot:
        self.calls += 1
        return RouterPositionSnapshot(
            portfolio_id=portfolio_id,
            instrument=instrument,
            position_id=self.position_id,
            side=self.side,
            quantity=self.quantity,
            observed_at=timestamp,
            source="core-position-test",
        )


class EventTimeMarkProvider:
    def mark_snapshot(
        self,
        instrument: str,
        timestamp: datetime,
    ) -> VenueMarkSnapshot:
        return VenueMarkSnapshot(
            instrument=instrument,
            price=Decimal("100"),
            observed_at=timestamp,
            source="router-add-test-mark",
        )


def _entry() -> EntryCandidate:
    return EntryCandidate(
        candidate_id="entry-add",
        source_decision_id="feature-entry-add",
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
        entry_kind="router-add-contract",
        order_type="limit",
        ttl_bars=12,
        price=Decimal("100"),
        limit_price=Decimal("100"),
        confidence=Decimal("0.8"),
    )


def _position_state() -> PositionState:
    return PositionState(
        position_id="position-a",
        symbol="BTCUSDT",
        side=TradeSide.LONG,
        quantity=BASELINE_QUANTITY,
        average_entry=Decimal("100"),
        version="position-v1",
        market="usdt_perpetual",
        strategy_id="strategy-a",
        setup_id="setup-a",
        opened_at=NOW - timedelta(hours=1),
    )


def _portfolio() -> PortfolioSnapshot:
    return PortfolioSnapshot(
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
        positions=(_position_state(),),
    )


def _add_envelope() -> DownstreamPlanEnvelope:
    entry = _entry()
    protection = ProtectionDecision(
        decision_id="protection-add",
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
        decision_id="sizing-add",
        request_id="request-add",
        setup_id=entry.setup_id,
        symbol=entry.symbol,
        approved=True,
        quantity=ADD_QUANTITY,
        notional=Decimal("1000"),
        risk_amount=Decimal("50"),
        risk_fraction=Decimal("0.005"),
        leverage=Decimal("1"),
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=10),
        entry_candidate_id=entry.candidate_id,
        protection_decision_id=protection.decision_id,
    )
    result = DecisionRouter(
        RouterConfiguration(existing_position_policy=ExistingPositionPolicy.ALLOW_SAME_SIDE_ADD)
    ).route(
        RouterInputPackage(
            as_of=NOW,
            portfolio=_portfolio(),
            entries=(entry,),
            protections=(protection,),
            sizings=(sizing,),
        )
    )
    assert result.plan is not None
    return DownstreamPlanEnvelope.from_plan(result.plan)


def _broker(metadata, *, fill_fraction: float) -> BrokerSimulator:
    broker = BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            allow_scale_in=True,
            execution=ExecutionConfig(max_fill_fraction_per_bar=fill_fraction),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )
    broker.portfolio.position = Position(
        id="position-a",
        entry_order_id="seed-order",
        side=PositionSide.LONG,
        quantity=float(BASELINE_QUANTITY),
        entry_price=100.0,
        entry_time=NOW - timedelta(hours=1),
        entry_index=-1,
        entry_fee=0.0,
        setup_id="setup-a",
    )
    return broker


def _coordinator(broker, metadata) -> PaperExecutionCoordinator:
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        mark_provider=EventTimeMarkProvider(),
        run_id="router-add-position",
    )


def _adapter(
    coordinator: PaperExecutionCoordinator,
    envelope: DownstreamPlanEnvelope,
    *,
    position_provider: StaticPositionProvider | None = None,
) -> RouterExecutionAdapter:
    return RouterExecutionAdapter(
        coordinator,
        occ_provider=StaticOCCProvider(envelope),
        position_provider=position_provider or StaticPositionProvider(),
    )


def _bar() -> Candle:
    return Candle(
        timestamp=NOW + timedelta(minutes=5),
        open=100.0,
        high=100.1,
        low=99.9,
        close=100.0,
        volume=100.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )


def _ingest_all(coordinator: PaperExecutionCoordinator, broker: BrokerSimulator) -> None:
    for event in tuple(broker.journal.orders):
        coordinator.ingest_order_event(event)


def test_factual_add_accepts_only_entry_and_registers_resulting_position_protection(
    versions: StateVersions,
    metadata,
) -> None:
    envelope = _add_envelope()
    broker = _broker(metadata, fill_fraction=1.0)
    coordinator = _coordinator(broker, metadata)

    result = _adapter(coordinator, envelope).accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )

    assert result.accepted
    assert len(result.client_order_ids) == 1
    assert len(broker.pending_snapshots()) == 1
    client_id = result.client_order_ids[0]
    deferred = coordinator.orchestration_state.protection_by_entry[client_id]
    assert deferred.baseline_position_quantity == BASELINE_QUANTITY
    assert deferred.entry_requested_quantity == ADD_QUANTITY
    assert deferred.total_position_quantity == BASELINE_QUANTITY
    assert {leg.planned_quantity for leg in deferred.legs.values()} == {
        BASELINE_QUANTITY + ADD_QUANTITY
    }
    assert deferred.pending_activations() == ()


def test_partial_add_fill_protects_baseline_plus_fill_and_cancels_remainder(
    versions: StateVersions,
    metadata,
) -> None:
    envelope = _add_envelope()
    broker = _broker(metadata, fill_fraction=0.5)
    coordinator = _coordinator(broker, metadata)
    result = _adapter(coordinator, envelope).accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )
    client_id = result.client_order_ids[0]

    broker.process_bar(_bar(), 1)
    _ingest_all(coordinator, broker)

    assert broker.portfolio.position is not None
    assert broker.portfolio.position.quantity == pytest.approx(10.0)
    assert broker.portfolio.position.stop_loss == pytest.approx(95.0)
    assert broker.portfolio.position.take_profit == pytest.approx(110.0)
    assert broker.pending_snapshots() == ()
    aggregate = coordinator.orders[client_id]
    deferred = coordinator.orchestration_state.protection_by_entry[client_id]
    assert aggregate.state is PaperOrderState.CANCELED
    assert deferred.cumulative_entry_fill == Decimal("5")
    assert deferred.total_position_quantity == Decimal("10")
    assert deferred.protected_quantity == Decimal("10")
    assert deferred.unprotected_quantity == Decimal("0")
    assert deferred.entry_terminal
    assert deferred.pending_activations() == ()


def test_full_add_fill_protects_exact_resulting_core_position(
    versions: StateVersions,
    metadata,
) -> None:
    envelope = _add_envelope()
    broker = _broker(metadata, fill_fraction=1.0)
    coordinator = _coordinator(broker, metadata)
    result = _adapter(coordinator, envelope).accept(
        envelope,
        now=NOW,
        current_index=0,
        mark_price=Decimal("100"),
        paper_versions=versions,
    )
    client_id = result.client_order_ids[0]

    broker.process_bar(_bar(), 1)
    _ingest_all(coordinator, broker)

    assert broker.portfolio.position is not None
    assert broker.portfolio.position.quantity == pytest.approx(15.0)
    assert broker.portfolio.position.stop_loss == pytest.approx(95.0)
    assert broker.portfolio.position.take_profit == pytest.approx(110.0)
    deferred = coordinator.orchestration_state.protection_by_entry[client_id]
    assert deferred.cumulative_entry_fill == ADD_QUANTITY
    assert deferred.total_position_quantity == Decimal("15")
    assert deferred.protected_quantity == Decimal("15")
    assert deferred.unprotected_quantity == Decimal("0")
    assert deferred.pending_activations() == ()


def test_factual_position_mismatch_rejects_before_journal_or_venue_side_effect(
    versions: StateVersions,
    metadata,
) -> None:
    envelope = _add_envelope()
    broker = _broker(metadata, fill_fraction=1.0)
    coordinator = _coordinator(broker, metadata)
    provider = StaticPositionProvider(quantity=Decimal("4"))

    with pytest.raises(PlanValidationError) as exc:
        _adapter(
            coordinator,
            envelope,
            position_provider=provider,
        ).accept(
            envelope,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=versions,
        )

    assert exc.value.reason_code is PaperReasonCode.STATE_VERSION_MISMATCH
    assert provider.calls == 1
    assert coordinator.journal.store.read() == ()
    assert broker.orders == []


def test_baseline_protection_schema_v5_round_trip() -> None:
    protection = ProtectionCoordinator(
        entry_client_order_id="entry-add",
        entry_requested_quantity=ADD_QUANTITY,
        baseline_position_quantity=BASELINE_QUANTITY,
    )
    protection.register_leg(
        DeferredProtectionLeg(
            client_order_id="stop-add",
            role=IntentRole.STOP,
            planned_quantity=Decimal("15"),
            trigger_price=Decimal("95"),
            activation_policy=(ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL),
        )
    )
    protection.register_leg(
        DeferredProtectionLeg(
            client_order_id="target-add",
            role=IntentRole.TARGET,
            planned_quantity=Decimal("15"),
            trigger_price=Decimal("110"),
            activation_policy=ProtectionActivationPolicy.AFTER_ENTRY_TERMINAL,
        )
    )
    protection.observe_entry_state(Decimal("5"), entry_terminal=True)
    protection.commit_activations(protection.pending_activations())

    state = protection.snapshot_state()
    restored = ProtectionCoordinator.from_state(state)

    assert state["schema_version"] == "5"
    assert restored.snapshot_state() == state
    assert restored.baseline_position_quantity == BASELINE_QUANTITY
    assert restored.total_position_quantity == Decimal("10")
    assert restored.protected_quantity == Decimal("10")
    assert restored.pending_activations() == ()


def test_add_protection_registration_failure_precedes_order_submission(
    versions: StateVersions,
    metadata,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    envelope = _add_envelope()
    broker = _broker(metadata, fill_fraction=1.0)
    coordinator = _coordinator(broker, metadata)

    def reject_registration(
        self: ExecutionOrchestrationState,
        protection: ProtectionCoordinator,
    ) -> None:
        del self, protection
        raise RuntimeError("injected registration failure")

    monkeypatch.setattr(
        ExecutionOrchestrationState,
        "register_protection",
        reject_registration,
    )

    with pytest.raises(ReconciliationBlocked):
        _adapter(coordinator, envelope).accept(
            envelope,
            now=NOW,
            current_index=0,
            mark_price=Decimal("100"),
            paper_versions=versions,
        )

    assert broker.orders == []
    assert coordinator._plan_results == {}
    assert not any(
        event.event_type == "plan.accepted" for event in coordinator.journal.store.read()
    )
