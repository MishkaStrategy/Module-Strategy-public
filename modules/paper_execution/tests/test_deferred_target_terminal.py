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
from modular_core.execution import BrokerSimulator

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperOrderState,
    PlanAction,
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


def _bracket_plan(plan, versions, now):
    entry = plan.intents[0]
    return ExecutableTradePlan.create(
        plan_id="partial-terminal-bracket",
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
                intent_id="stop-terminal",
                role=IntentRole.STOP,
                side=Side.SELL,
                order_type=OrderType.STOP_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("99000"),
                reduce_only=True,
                oco_group_id="terminal-bracket",
            ),
            ExecutionIntent(
                intent_id="target-terminal",
                role=IntentRole.TARGET,
                side=Side.SELL,
                order_type=OrderType.TAKE_PROFIT_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("110000"),
                reduce_only=True,
                oco_group_id="terminal-bracket",
            ),
        ),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=PaperExecutionConfig().canonical_hash,
        input_hashes={"decision": "d" * 64},
    )


def test_partial_fill_cancel_is_reflected_before_late_core_events(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )
    service = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        mark_provider=EventTimeMarkProvider(),
        run_id="partial-terminal",
    )
    result = service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    entry_client_id = result.client_order_ids[0]

    broker.process_bar(
        Candle(
            timestamp=now + timedelta(minutes=5),
            open=100000,
            high=100100,
            low=99900,
            close=100000,
            volume=10,
            symbol="BTCUSDT",
            timeframe="5m",
        ),
        1,
    )
    fill_events = tuple(broker.journal.orders)
    for event in fill_events:
        service.ingest_order_event(event)

    deferred = service.orchestration_state.protection_by_entry[entry_client_id]
    assert deferred.entry_terminal
    assert deferred.cumulative_entry_fill > 0
    assert deferred.pending_activations() == ()
    assert service.orders[entry_client_id].state is PaperOrderState.CANCELED
    assert broker.portfolio.position is not None
    assert broker.portfolio.position.stop_loss == pytest.approx(99000)
    assert broker.portfolio.position.take_profit == pytest.approx(110000)
    assert broker.pending_snapshots() == ()

    late_cancel_events = tuple(broker.journal.orders)[len(fill_events) :]
    assert [event.event for event in late_cancel_events] == [
        "cancel_pending",
        "cancelled",
    ]
    assert all(service.ingest_order_event(event) is False for event in late_cancel_events)
    assert service.orders[entry_client_id].state is PaperOrderState.CANCELED
