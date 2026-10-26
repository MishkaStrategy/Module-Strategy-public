from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from modular_core import Candle, OrderType, Side

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperReasonCode,
    PlanAction,
    PlanValidationError,
    ReconciliationBlocked,
)


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def coordinator(broker, metadata):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="run-integration",
    )


def test_coordinator_submits_to_existing_core_broker(plan, versions, now, broker, metadata) -> None:
    service = coordinator(broker, metadata)
    result = service.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert result.accepted
    assert len(result.client_order_ids) == 1
    assert broker.pending_snapshots()[0].client_order_id == result.client_order_ids[0]
    assert service.journal.verify_chain()


def test_duplicate_plan_returns_existing_result_without_new_order(
    plan, versions, now, broker, metadata
) -> None:
    service = coordinator(broker, metadata)
    first = service.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    second = service.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    assert first is second
    assert len(broker.orders) == 1


def test_same_plan_id_with_different_content_is_critical_conflict(
    plan, versions, now, broker, metadata
) -> None:
    service = coordinator(broker, metadata)
    service.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    different = ExecutableTradePlan.create(
        plan_id=plan.plan_id,
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=plan.action,
        intents=(replace(plan.intents[0], quantity=Decimal("0.02")),),
        expected_versions=plan.expected_versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
    )
    with pytest.raises(PlanValidationError) as exc:
        service.accept_plan(
            different, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
        )
    assert exc.value.reason_code is PaperReasonCode.PLAN_ID_CONTENT_MISMATCH


def test_core_partial_fill_events_are_ingested(plan, versions, now, broker, metadata) -> None:
    service = coordinator(broker, metadata)
    result = service.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    bar = Candle(
        timestamp=now + timedelta(minutes=5),
        open=100000,
        high=100100,
        low=99000,
        close=100000,
        volume=10,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    broker.process_bar(bar, 1)
    client_id = result.client_order_ids[0]
    events = [item for item in broker.journal.orders if item.order.client_order_id == client_id]
    assert any(item.event == "partially_filled" for item in events)
    for item in events:
        service.ingest_order_event(item)
    assert service.orders[client_id].filled_quantity > 0


def test_failed_venue_submission_returns_structured_result(
    plan, versions, now, broker, metadata
) -> None:
    service = coordinator(broker, metadata)
    bad = ExecutableTradePlan.create(
        plan_id="bad-notional",
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=(
            ExecutionIntent(
                intent_id="tiny",
                role=IntentRole.ENTRY,
                side=Side.BUY,
                order_type=OrderType.LIMIT,
                quantity=Decimal("0.001"),
                limit_price=Decimal("100"),
            ),
        ),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
    )
    result = service.accept_plan(
        bad, now=now, current_index=0, mark_price=Decimal("100"), versions=versions
    )
    assert not result.accepted
    assert result.terminal
    assert result.reason_code is PaperReasonCode.VENUE_RULE_REJECTED


def test_reconciliation_blocks_scope_after_missing_venue_order(
    plan, versions, now, broker, metadata
) -> None:
    service = coordinator(broker, metadata)
    service.accept_plan(
        plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
    )
    broker._pending.clear()
    report = service.reconcile(portfolio_id=plan.portfolio_id, instrument=plan.instrument)
    assert report.blocked
    with pytest.raises(ReconciliationBlocked):
        service.accept_plan(
            ExecutableTradePlan.create(
                plan_id="plan-2",
                created_at=now,
                valid_from=now,
                expires_at=now + timedelta(minutes=1),
                portfolio_id=plan.portfolio_id,
                instrument=plan.instrument,
                market=plan.market,
                action=plan.action,
                intents=plan.intents,
                expected_versions=versions,
                metadata_version=plan.metadata_version,
                configuration_hash=plan.configuration_hash,
            ),
            now=now,
            current_index=1,
            mark_price=Decimal("100000"),
            versions=versions,
        )


def test_configuration_hash_mismatch_is_rejected_before_submission(
    plan, versions, now, broker, metadata
) -> None:
    service = PaperExecutionCoordinator(
        config=PaperExecutionConfig(maximum_open_orders=99),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="run-config-mismatch",
    )
    with pytest.raises(PlanValidationError) as exc:
        service.accept_plan(
            plan, now=now, current_index=0, mark_price=Decimal("100000"), versions=versions
        )
    assert exc.value.reason_code is PaperReasonCode.INVALID_CONFIGURATION
    assert broker.pending_snapshots() == ()


def test_missing_cancel_target_returns_structured_failure(
    plan, versions, now, broker, metadata
) -> None:
    service = coordinator(broker, metadata)
    cancel_plan = ExecutableTradePlan.create(
        plan_id="cancel-missing",
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.CANCEL_ORDERS,
        intents=(
            ExecutionIntent(
                intent_id="cancel-1",
                role=IntentRole.CANCEL,
                cancel_client_order_id="pevr-missing-order",
            ),
        ),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
    )
    result = service.accept_plan(
        cancel_plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert not result.accepted
    assert result.reason_code is PaperReasonCode.CANCEL_TARGET_NOT_FOUND


def test_invalid_second_intent_is_rejected_before_any_submission(
    plan, versions, now, broker, metadata
) -> None:
    service = coordinator(broker, metadata)
    second = ExecutionIntent(
        intent_id="bad-second",
        role=IntentRole.ENTRY,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.001"),
        limit_price=Decimal("100"),
    )
    multi = ExecutableTradePlan.create(
        plan_id="multi-preflight",
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=(plan.intents[0], second),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
    )
    result = service.accept_plan(
        multi,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert not result.accepted
    assert result.client_order_ids == ()
    assert broker.orders == []
    assert broker.pending_snapshots() == ()
    assert service.journal.verify_chain()


class RejectSecondVenue:
    def __init__(self, broker) -> None:
        self.broker = broker
        self.submissions = 0

    def submit(self, request, **kwargs):
        self.submissions += 1
        if self.submissions == 2:
            return None
        return self.broker.submit(request, **kwargs)

    def cancel(self, request, **kwargs):
        return self.broker.cancel(request, **kwargs)

    def pending_snapshots(self):
        return self.broker.pending_snapshots()

    def process_bar(self, bar, index):
        self.broker.process_bar(bar, index)


def test_runtime_second_submission_failure_compensates_first(
    plan, versions, now, broker, metadata
) -> None:
    venue = RejectSecondVenue(broker)
    service = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=venue,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="runtime-compensation",
    )
    second = replace(plan.intents[0], intent_id="entry-2", quantity=Decimal("0.02"))
    multi = ExecutableTradePlan.create(
        plan_id="multi-runtime-rollback",
        created_at=plan.created_at,
        valid_from=plan.valid_from,
        expires_at=plan.expires_at,
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=(plan.intents[0], second),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=plan.configuration_hash,
    )
    result = service.accept_plan(
        multi,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert not result.accepted
    assert result.reason_code is PaperReasonCode.ORDER_REJECTED
    assert len(result.client_order_ids) == 1
    assert broker.pending_snapshots() == ()
    assert service.orders[result.client_order_ids[0]].state.value == "canceled"
