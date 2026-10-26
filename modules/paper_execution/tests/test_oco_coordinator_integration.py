from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from modular_core import (
    CancelRequest,
    EngineConfig,
    Journal,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
)
from modular_core.execution import BrokerSimulator
from modular_core.models import Position

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperOrderState,
    PlanAction,
    ReconciliationBlocked,
)


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument: str, timestamp: datetime):
        assert instrument == self.metadata.instrument
        return self.metadata


class CancelControllableBroker(BrokerSimulator):
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.cancel_failure_for: str | None = None
        self.cancel_attempts: list[str] = []

    def cancel(
        self,
        request: CancelRequest,
        *,
        timestamp: datetime,
        source_module: str,
    ) -> int:
        client_id = request.client_order_id
        if client_id is not None:
            self.cancel_attempts.append(client_id)
        if client_id == self.cancel_failure_for:
            return 0
        return super().cancel(
            request,
            timestamp=timestamp,
            source_module=source_module,
        )


def _broker(metadata) -> CancelControllableBroker:
    broker = CancelControllableBroker(
        EngineConfig(one_active_order=False),
        Journal(),
        instrument=metadata.to_core_spec(),
    )
    broker.portfolio.position = Position(
        id="position-oco",
        entry_order_id="seed-order",
        side=PositionSide.LONG,
        quantity=1.0,
        entry_price=100.0,
        entry_time=metadata.effective_from,
        entry_index=-1,
        entry_fee=0.0,
        setup_id="setup-oco",
    )
    return broker


def _plan(now, versions) -> ExecutableTradePlan:
    config = PaperExecutionConfig()
    return ExecutableTradePlan.create(
        plan_id="plan-oco",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="binance_usdtm",
        action=PlanAction.REDUCE_POSITION,
        intents=(
            ExecutionIntent(
                intent_id="stop-oco",
                role=IntentRole.STOP,
                side=Side.SELL,
                order_type=OrderType.STOP_MARKET,
                quantity=Decimal("1"),
                trigger_price=Decimal("95"),
                reduce_only=True,
                position_side=PositionSide.LONG,
                oco_group_id="oco-main",
            ),
            ExecutionIntent(
                intent_id="target-oco",
                role=IntentRole.TARGET,
                side=Side.SELL,
                order_type=OrderType.TAKE_PROFIT_MARKET,
                quantity=Decimal("1"),
                trigger_price=Decimal("110"),
                reduce_only=True,
                position_side=PositionSide.LONG,
                oco_group_id="oco-main",
            ),
        ),
        expected_versions=versions,
        metadata_version="binance-usdtm-2026-01-01",
        configuration_hash=config.canonical_hash,
        input_hashes={"decision": "b" * 64},
    )


def _coordinator(broker, metadata) -> PaperExecutionCoordinator:
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="oco-integration",
    )


def _accept(coordinator, plan, now, versions):
    result = coordinator.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100"),
        versions=versions,
    )
    assert result.accepted
    roles = {
        coordinator.orders[client_id].intent_role: client_id
        for client_id in result.client_order_ids
    }
    return result, roles


def _fill_event(
    broker: BrokerSimulator,
    client_id: str,
    now: datetime,
    *,
    cumulative: float,
    event_id: str,
) -> OrderEvent:
    snapshot = next(row for row in broker.pending_snapshots() if row.client_order_id == client_id)
    status = OrderStatus.FILLED if cumulative == snapshot.quantity else OrderStatus.PARTIALLY_FILLED
    return OrderEvent(
        timestamp=now,
        event=("filled" if status is OrderStatus.FILLED else "partially_filled"),
        order=replace(
            snapshot,
            status=status,
            filled_quantity=cumulative,
            remaining_quantity=snapshot.quantity - cumulative,
            average_fill_price=110.0,
        ),
        event_id=event_id,
    )


def test_full_oco_fill_cancels_sibling_only_after_venue_confirmation(
    now,
    versions,
    metadata,
) -> None:
    broker = _broker(metadata)
    coordinator = _coordinator(broker, metadata)
    _, roles = _accept(coordinator, _plan(now, versions), now, versions)

    assert coordinator.ingest_order_event(
        _fill_event(
            broker,
            roles[IntentRole.TARGET.value],
            now + timedelta(seconds=1),
            cumulative=1.0,
            event_id="target-full",
        )
    )

    stop_id = roles[IntentRole.STOP.value]
    group = coordinator.orchestration_state.oco_groups["oco-main"]
    assert broker.cancel_attempts == [stop_id]
    assert group.canceled == {stop_id}
    assert coordinator.orders[stop_id].state is PaperOrderState.CANCELED
    assert ("portfolio-main", "BTCUSDT") not in coordinator.blocked_scopes


def test_partial_oco_fill_cancels_overclose_risk_and_blocks_residual_scope(
    now,
    versions,
    metadata,
) -> None:
    broker = _broker(metadata)
    coordinator = _coordinator(broker, metadata)
    _, roles = _accept(coordinator, _plan(now, versions), now, versions)

    coordinator.ingest_order_event(
        _fill_event(
            broker,
            roles[IntentRole.TARGET.value],
            now + timedelta(seconds=1),
            cumulative=0.4,
            event_id="target-partial",
        )
    )

    stop_id = roles[IntentRole.STOP.value]
    group = coordinator.orchestration_state.oco_groups["oco-main"]
    assert broker.cancel_attempts == [stop_id]
    assert group.canceled == {stop_id}
    assert coordinator.orders[stop_id].state is PaperOrderState.CANCELED
    assert ("portfolio-main", "BTCUSDT") in coordinator.blocked_scopes
    assert any(
        event.event_type == "oco.residual_protection_blocked"
        for event in coordinator.journal.store.read()
    )


def test_oco_cancel_failure_never_records_false_cancellation_and_blocks_scope(
    now,
    versions,
    metadata,
) -> None:
    broker = _broker(metadata)
    coordinator = _coordinator(broker, metadata)
    _, roles = _accept(coordinator, _plan(now, versions), now, versions)
    stop_id = roles[IntentRole.STOP.value]
    broker.cancel_failure_for = stop_id

    coordinator.ingest_order_event(
        _fill_event(
            broker,
            roles[IntentRole.TARGET.value],
            now + timedelta(seconds=1),
            cumulative=1.0,
            event_id="target-full-cancel-fails",
        )
    )

    group = coordinator.orchestration_state.oco_groups["oco-main"]
    assert stop_id not in group.canceled
    assert coordinator.orders[stop_id].state is not PaperOrderState.CANCELED
    assert ("portfolio-main", "BTCUSDT") in coordinator.blocked_scopes
    assert any(
        event.event_type == "oco.adjustment_failed" for event in coordinator.journal.store.read()
    )


def test_duplicate_oco_fill_is_idempotent_and_does_not_repeat_cancel(
    now,
    versions,
    metadata,
) -> None:
    broker = _broker(metadata)
    coordinator = _coordinator(broker, metadata)
    _, roles = _accept(coordinator, _plan(now, versions), now, versions)
    event = _fill_event(
        broker,
        roles[IntentRole.TARGET.value],
        now + timedelta(seconds=1),
        cumulative=1.0,
        event_id="target-duplicate",
    )

    assert coordinator.ingest_order_event(event)
    assert not coordinator.ingest_order_event(event)
    assert broker.cancel_attempts == [roles[IntentRole.STOP.value]]


def test_late_fill_after_confirmed_oco_cancel_blocks_scope(
    now,
    versions,
    metadata,
) -> None:
    broker = _broker(metadata)
    coordinator = _coordinator(broker, metadata)
    _, roles = _accept(coordinator, _plan(now, versions), now, versions)
    target_id = roles[IntentRole.TARGET.value]
    stop_id = roles[IntentRole.STOP.value]
    coordinator.ingest_order_event(
        _fill_event(
            broker,
            target_id,
            now + timedelta(seconds=1),
            cumulative=1.0,
            event_id="target-closes-position",
        )
    )

    canceled_snapshot = next(row for row in broker.orders if row.client_order_id == stop_id)
    late = OrderEvent(
        timestamp=now + timedelta(seconds=2),
        event="filled",
        order=replace(
            OrderSnapshot.from_order(canceled_snapshot),
            status=OrderStatus.FILLED,
            filled_quantity=1.0,
            remaining_quantity=0.0,
            average_fill_price=95.0,
        ),
        event_id="late-stop-fill",
    )

    with pytest.raises(ReconciliationBlocked):
        coordinator.ingest_order_event(late)
    assert ("portfolio-main", "BTCUSDT") in coordinator.blocked_scopes
