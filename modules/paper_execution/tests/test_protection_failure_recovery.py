from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest
from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    Journal,
    OrderType,
    Side,
)
from modular_core.events import InMemoryEventStore
from modular_core.execution import BrokerSimulator

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperOrderState,
    PlanAction,
    ReconciliationBlocked,
    VenueMarkSnapshot,
)


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _broker(metadata, *, fill_fraction: float) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=fill_fraction),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )


def _plan(plan, versions, now):
    entry = plan.intents[0]
    return ExecutableTradePlan.create(
        plan_id="failure-recovery-bracket",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=10),
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=(
            entry,
            ExecutionIntent(
                intent_id="failure-stop",
                role=IntentRole.STOP,
                side=Side.SELL,
                order_type=OrderType.STOP_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("99000"),
                reduce_only=True,
                oco_group_id="failure-bracket",
            ),
            ExecutionIntent(
                intent_id="failure-target",
                role=IntentRole.TARGET,
                side=Side.SELL,
                order_type=OrderType.TAKE_PROFIT_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("110000"),
                reduce_only=True,
                oco_group_id="failure-bracket",
            ),
        ),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=PaperExecutionConfig().canonical_hash,
        input_hashes={"decision": "e" * 64},
    )


def _bar(now):
    return Candle(
        timestamp=now + timedelta(minutes=5),
        open=100000,
        high=100100,
        low=99900,
        close=100000,
        volume=10,
        symbol="BTCUSDT",
        timeframe="5m",
    )


class ToggleVenue:
    def __init__(
        self,
        broker,
        *,
        reject_update: bool = False,
        reject_cancel: bool = False,
    ) -> None:
        self.broker = broker
        self.reject_update = reject_update
        self.reject_cancel = reject_cancel

    def submit(self, request, **kwargs):
        return self.broker.submit(request, **kwargs)

    def cancel(self, request, **kwargs):
        if self.reject_cancel:
            return 0
        return self.broker.cancel(request, **kwargs)

    def pending_snapshots(self):
        return self.broker.pending_snapshots()

    def process_bar(self, bar, index):
        return self.broker.process_bar(bar, index)

    def mark_snapshot(self, instrument, timestamp):
        return VenueMarkSnapshot(
            instrument=instrument,
            price=Decimal("100000"),
            observed_at=timestamp,
            source="test-event-time-mark",
        )

    def update_position(self, request, **kwargs):
        if self.reject_update:
            return False
        return self.broker.update_position(request, **kwargs)


def _service(venue, metadata, store, run_id):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=venue,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )


def test_pending_activation_survives_coordinator_snapshot_and_retry(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata, fill_fraction=1.0)
    venue = ToggleVenue(broker, reject_update=True)
    store = InMemoryEventStore()
    source = _service(venue, metadata, store, "activation-restart")
    result = source.accept_plan(
        _plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    client_id = result.client_order_ids[0]

    broker.process_bar(_bar(now), 1)
    events = tuple(broker.journal.orders)
    source.ingest_order_event(events[0])
    with pytest.raises(ReconciliationBlocked):
        for event in events[1:]:
            source.ingest_order_event(event)

    state = source.snapshot_state()
    recovered = _service(venue, metadata, store, "activation-restart")
    recovered.restore_state(state)
    deferred = recovered.orchestration_state.protection_by_entry[client_id]
    assert deferred.cumulative_entry_fill > 0
    assert deferred.protected_quantity == Decimal("0")
    assert deferred.pending_activations()

    venue.reject_update = False
    assert recovered.retry_pending_protection(
        client_order_id=client_id,
        timestamp=now + timedelta(minutes=6),
    )
    assert deferred.pending_activations() == ()
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit == pytest.approx(110000)
    assert recovered.recovery_pending


def test_cancel_failure_keeps_stop_and_retryable_cancel_intent(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata, fill_fraction=0.5)
    venue = ToggleVenue(broker, reject_cancel=True)
    service = _service(
        venue,
        metadata,
        InMemoryEventStore(),
        "cancel-retry",
    )
    result = service.accept_plan(
        _plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    client_id = result.client_order_ids[0]

    broker.process_bar(_bar(now), 1)
    events = tuple(broker.journal.orders)
    service.ingest_order_event(events[0])
    with pytest.raises(ReconciliationBlocked, match="could not cancel"):
        for event in events[1:]:
            service.ingest_order_event(event)

    aggregate = service.orders[client_id]
    deferred = service.orchestration_state.protection_by_entry[client_id]
    assert aggregate.state is PaperOrderState.CANCEL_PENDING
    assert deferred.protected_quantity == aggregate.filled_quantity
    assert deferred.entry_terminal is False
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit is None

    venue.reject_cancel = False
    assert service.retry_pending_protection(
        client_order_id=client_id,
        timestamp=now + timedelta(minutes=6),
    )
    assert aggregate.state is PaperOrderState.CANCELED
    assert deferred.entry_terminal
    assert deferred.pending_activations() == ()
    assert broker.pending_snapshots() == ()
    assert broker.portfolio.position.take_profit == pytest.approx(110000)
