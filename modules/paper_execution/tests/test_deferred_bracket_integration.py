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
from modular_core.events import InMemoryEventStore, JsonLinesEventStore
from modular_core.execution import BrokerSimulator

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    LifecycleState,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperOrderState,
    PaperReasonCode,
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


class EventTimeMarkProvider:
    def mark_snapshot(self, instrument, timestamp):
        return VenueMarkSnapshot(
            instrument=instrument,
            price=Decimal("100000"),
            observed_at=timestamp,
            source="test-event-time-mark",
        )


def _broker(metadata, *, fill_fraction: float) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=fill_fraction),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )


def _coordinator(venue, metadata, *, store=None, run_id="deferred-bracket"):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=venue,
        metadata_provider=StaticMetadataProvider(metadata),
        mark_provider=EventTimeMarkProvider(),
        run_id=run_id,
        event_store=store,
    )


class _ProtectionAppendFaultStore:
    def __init__(self, delegate, *, after_write: bool) -> None:
        self.delegate = delegate
        self.after_write = after_write
        self.failed = False

    def append(self, event) -> None:
        if not self.failed and event.event_type == "protection.deferred":
            self.failed = True
            if not self.after_write:
                raise OSError("synthetic deferred event failure before write")
            self.delegate.append(event)
            raise OSError("synthetic deferred event fsync acknowledgement lost")
        self.delegate.append(event)

    def append_many(self, events) -> None:
        for event in events:
            self.append(event)

    def read(self, *, after_sequence: int = 0):
        return self.delegate.read(after_sequence=after_sequence)


def _bracket_plan(
    plan,
    versions,
    now,
    *,
    extra_target: bool = False,
    include_stop: bool = True,
):
    entry = plan.intents[0]
    intents = [entry]
    if include_stop:
        intents.append(
            ExecutionIntent(
                intent_id="stop-1",
                role=IntentRole.STOP,
                side=Side.SELL,
                order_type=OrderType.STOP_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("99000"),
                reduce_only=True,
                oco_group_id="bracket-1",
            )
        )
    intents.append(
        ExecutionIntent(
            intent_id="target-1",
            role=IntentRole.TARGET,
            side=Side.SELL,
            order_type=OrderType.TAKE_PROFIT_MARKET,
            quantity=entry.quantity,
            trigger_price=Decimal("110000"),
            reduce_only=True,
            oco_group_id="bracket-1",
        )
    )
    if extra_target:
        intents.append(
            ExecutionIntent(
                intent_id="target-2",
                role=IntentRole.TARGET,
                side=Side.SELL,
                order_type=OrderType.TAKE_PROFIT_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("115000"),
                reduce_only=True,
                oco_group_id="bracket-1",
            )
        )
    return ExecutableTradePlan.create(
        plan_id="plan-bracket",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=tuple(intents),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=PaperExecutionConfig().canonical_hash,
        input_hashes={"decision": "c" * 64},
    )


def _fill_bar(now):
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


def _ingest_events(service, events) -> None:
    for event in events:
        service.ingest_order_event(event)


def test_bracket_intents_are_deferred_until_entry_fill(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata, fill_fraction=0.5)
    service = _coordinator(broker, metadata)
    result = service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert result.accepted
    assert len(result.client_order_ids) == 1
    assert len(broker.pending_snapshots()) == 1
    assert set(service.orders) == {result.client_order_ids[0]}
    deferred = service.orchestration_state.protection_by_entry[result.client_order_ids[0]]
    assert {leg.role for leg in deferred.legs.values()} == {
        IntentRole.STOP,
        IntentRole.TARGET,
    }
    assert deferred.pending_activations() == ()


@pytest.mark.parametrize("after_write", [False, True])
def test_deferred_protection_append_failure_clears_acceptance_cache_and_stays_blocked(
    tmp_path,
    plan,
    versions,
    now,
    metadata,
    after_write,
) -> None:
    path = tmp_path / "paper-events.jsonl"
    raw_store = JsonLinesEventStore(path, fsync=True)
    store = _ProtectionAppendFaultStore(raw_store, after_write=after_write)
    broker = _broker(metadata, fill_fraction=0.5)
    service = _coordinator(broker, metadata, store=store, run_id="deferred-fault")
    bracket_plan = _bracket_plan(plan, versions, now)
    scope = (bracket_plan.portfolio_id, bracket_plan.instrument)

    with pytest.raises(ReconciliationBlocked, match="deferred protection registration failed"):
        service.accept_plan(
            bracket_plan,
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )

    assert bracket_plan.plan_id not in service._plan_results
    assert bracket_plan.plan_id not in service._plan_hashes
    assert scope in service.blocked_scopes
    assert service.lifecycle is LifecycleState.BLOCKED
    events = tuple(raw_store.read())
    event_types = tuple(event.event_type for event in events)
    assert event_types.count("order.submitted") == 1
    assert event_types.count("plan.accepted") == 1
    assert event_types.count("protection.deferred") == int(after_write)
    assert "execution.failed" not in event_types
    assert len(broker.pending_snapshots()) == 1

    with pytest.raises(ReconciliationBlocked):
        service.accept_plan(
            bracket_plan,
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )
    assert len(broker.pending_snapshots()) == 1
    assert tuple(event.event_type for event in raw_store.read()).count("order.submitted") == 1

    reopened = _coordinator(
        broker,
        metadata,
        store=JsonLinesEventStore(path, fsync=True),
        run_id="deferred-fault",
    )
    assert reopened.lifecycle is LifecycleState.BLOCKED
    with pytest.raises(ReconciliationBlocked):
        reopened.accept_plan(
            bracket_plan,
            now=now,
            current_index=0,
            mark_price=Decimal("100000"),
            versions=versions,
        )
    assert len(broker.pending_snapshots()) == 1


def test_partial_fill_is_protected_and_remainder_is_closed_synchronously(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata, fill_fraction=0.5)
    service = _coordinator(broker, metadata)
    result = service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    client_id = result.client_order_ids[0]

    broker.process_bar(_fill_bar(now), 1)
    fill_events = tuple(broker.journal.orders)
    _ingest_events(service, fill_events)

    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit == pytest.approx(110000)
    assert broker.pending_snapshots() == ()
    aggregate = service.orders[client_id]
    assert aggregate.state is PaperOrderState.CANCELED
    deferred = service.orchestration_state.protection_by_entry[client_id]
    assert deferred.entry_terminal
    assert deferred.unprotected_quantity == Decimal("0")
    assert deferred.pending_activations() == ()

    late_confirmations = tuple(broker.journal.orders)[len(fill_events) :]
    assert [event.event for event in late_confirmations] == [
        "cancel_pending",
        "cancelled",
    ]
    assert all(service.ingest_order_event(event) is False for event in late_confirmations)
    assert service.orders[client_id].state is PaperOrderState.CANCELED


def test_full_fill_activates_stop_and_target(plan, versions, now, metadata) -> None:
    broker = _broker(metadata, fill_fraction=1.0)
    service = _coordinator(broker, metadata)
    service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    broker.process_bar(_fill_bar(now), 1)
    _ingest_events(service, tuple(broker.journal.orders))

    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit == pytest.approx(110000)


def test_restart_before_fill_preserves_deferred_activation(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata, fill_fraction=1.0)
    store = InMemoryEventStore()
    original = _coordinator(
        broker,
        metadata,
        store=store,
        run_id="bracket-restart",
    )
    original.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    state = original.snapshot_state()

    recovered = _coordinator(
        broker,
        metadata,
        store=store,
        run_id="bracket-restart",
    )
    recovered.restore_state(state)
    broker.process_bar(_fill_bar(now), 1)
    _ingest_events(recovered, tuple(broker.journal.orders))

    assert recovered.recovery_pending
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit == pytest.approx(110000)


class ToggleProtectionVenue:
    def __init__(self, broker, *, reject_update: bool = True) -> None:
        self.broker = broker
        self.reject_update = reject_update

    def submit(self, request, **kwargs):
        return self.broker.submit(request, **kwargs)

    def cancel(self, request, **kwargs):
        return self.broker.cancel(request, **kwargs)

    def pending_snapshots(self):
        return self.broker.pending_snapshots()

    def process_bar(self, bar, index):
        return self.broker.process_bar(bar, index)

    def update_position(self, request, **kwargs):
        if self.reject_update:
            return False
        return self.broker.update_position(request, **kwargs)


def test_failed_update_preserves_factual_fill_and_retryable_activation(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata, fill_fraction=1.0)
    venue = ToggleProtectionVenue(broker)
    service = _coordinator(venue, metadata)
    result = service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    client_id = result.client_order_ids[0]

    broker.process_bar(_fill_bar(now), 1)
    events = tuple(broker.journal.orders)
    service.ingest_order_event(events[0])
    with pytest.raises(
        ReconciliationBlocked,
        match="rejected deferred protection",
    ):
        for event in events[1:]:
            service.ingest_order_event(event)

    scope = (plan.portfolio_id, plan.instrument)
    assert scope in service.blocked_scopes
    assert service.lifecycle is LifecycleState.BLOCKED
    deferred = service.orchestration_state.protection_by_entry[client_id]
    assert deferred.cumulative_entry_fill == service.orders[client_id].filled_quantity
    assert deferred.cumulative_entry_fill > 0
    assert deferred.protected_quantity == Decimal("0")
    assert deferred.pending_activations()
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss is None

    venue.reject_update = False
    assert service.retry_pending_protection(
        client_order_id=client_id,
        timestamp=now + timedelta(minutes=6),
    )
    assert deferred.pending_activations() == ()
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit == pytest.approx(110000)


@pytest.mark.parametrize(
    ("extra_target", "include_stop"),
    ((True, True), (False, False)),
)
def test_ambiguous_or_unprotected_brackets_fail_before_submission(
    plan,
    versions,
    now,
    metadata,
    extra_target,
    include_stop,
) -> None:
    broker = _broker(metadata, fill_fraction=1.0)
    service = _coordinator(broker, metadata)
    result = service.accept_plan(
        _bracket_plan(
            plan,
            versions,
            now,
            extra_target=extra_target,
            include_stop=include_stop,
        ),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert not result.accepted
    assert result.reason_code is PaperReasonCode.INVALID_PLAN
    assert broker.orders == []
