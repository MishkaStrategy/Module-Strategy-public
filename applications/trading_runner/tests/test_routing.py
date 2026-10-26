from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from modular_core import (
    EventEnvelope,
    InMemoryEventStore,
    InMemoryStateRepository,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    PositionSide,
    Side,
    TimeInForce,
    Trade,
)

from modular_trading_runner.errors import (
    FactualEventIdentityConflict,
    FactualEventRecoveryRequired,
)
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.routing import (
    FactualEventEnvelope,
    FactualEventKind,
    FactualEventRouter,
)

NOW = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.value = NOW

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


class Consumer:
    def __init__(self) -> None:
        self.order_calls = 0
        self.trade_calls = 0
        self.fail_order_once = False

    def on_order_event(self, event: OrderEvent) -> None:
        self.order_calls += 1
        if self.fail_order_once:
            self.fail_order_once = False
            raise RuntimeError("order consumer failed")

    def on_trade(self, trade: Trade) -> None:
        self.trade_calls += 1

    def snapshot_state(self) -> Mapping[str, object]:
        return {"order_calls": self.order_calls, "trade_calls": self.trade_calls}

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.order_calls = int(state["order_calls"])
        self.trade_calls = int(state["trade_calls"])


class Evidence:
    def snapshot_evidence(self) -> Mapping[str, object]:
        return {"portfolio_version": "portfolio-v1"}


class FailTerminalJournal(RuntimeEventJournal):
    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)
        self.fail_terminal_once = True

    def append(self, **kwargs: Any) -> EventEnvelope:
        if kwargs.get("event_type") == "factual_event.processed" and self.fail_terminal_once:
            self.fail_terminal_once = False
            raise RuntimeError("terminal journal failure")
        return super().append(**kwargs)


def order_event(*, event: str = "filled") -> OrderEvent:
    order = OrderSnapshot(
        id="order-1",
        source_module="paper_execution",
        created_at=NOW,
        side=Side.BUY,
        order_type=OrderType.MARKET,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag="",
        setup_id=None,
        client_order_id="paper-order-1",
        status=OrderStatus.FILLED,
        filled_quantity=1.0,
        remaining_quantity=0.0,
        average_fill_price=100.0,
        fees_paid=0.05,
        time_in_force=TimeInForce.GTC,
    )
    return OrderEvent(
        timestamp=NOW,
        event=event,
        order=order,
        event_id="core-order-event-1",
        correlation_id="plan-1",
        causation_id="paper-event-1",
    )


def trade() -> Trade:
    return Trade(
        side=PositionSide.LONG,
        quantity=1.0,
        entry_time=NOW - timedelta(minutes=5),
        exit_time=NOW,
        entry_price=100.0,
        exit_price=105.0,
        gross_pnl=5.0,
        fees=0.1,
        net_pnl=4.9,
        exit_reason="take_profit",
        position_id="position-1",
    )


def order_envelope(*, event_id: str = "factual-order-1") -> FactualEventEnvelope:
    payload = order_event()
    return FactualEventEnvelope(
        event_id=event_id,
        aggregate_id="portfolio-1:BTCUSDT",
        symbol="BTCUSDT",
        timestamp=payload.timestamp,
        kind=FactualEventKind.ORDER,
        payload=payload,
        correlation_id=payload.correlation_id,
        causation_id=payload.causation_id,
    )


def trade_envelope(*, event_id: str = "factual-trade-1") -> FactualEventEnvelope:
    payload = trade()
    return FactualEventEnvelope(
        event_id=event_id,
        aggregate_id="portfolio-1:BTCUSDT",
        symbol="BTCUSDT",
        timestamp=payload.exit_time,
        kind=FactualEventKind.TRADE,
        payload=payload,
        correlation_id="plan-1",
        causation_id="fill-1",
    )


def router(
    *,
    consumer: Consumer,
    repository: InMemoryStateRepository,
    journal: RuntimeEventJournal,
    clock: Clock,
) -> FactualEventRouter:
    return FactualEventRouter(
        run_id="run-1",
        config_hash="a" * 64,
        module_graph_hash="b" * 64,
        consumer=consumer,
        state_repository=repository,
        journal=journal,
        clock=clock,
        queue_capacity=8,
        portfolio_evidence=Evidence(),
    )


def test_factual_envelope_roundtrip_preserves_core_dto_and_hash() -> None:
    for original in (order_envelope(), trade_envelope()):
        restored = FactualEventEnvelope.from_mapping(original.to_mapping())
        assert restored == original
        assert restored.content_hash == original.content_hash


def test_order_and_trade_route_once_with_portfolio_evidence() -> None:
    consumer = Consumer()
    clock = Clock()
    journal = RuntimeEventJournal(run_id="run-1", store=InMemoryEventStore())
    coordinator = router(
        consumer=consumer,
        repository=InMemoryStateRepository(),
        journal=journal,
        clock=clock,
    )
    order_result = coordinator.route(order_envelope())
    trade_result = coordinator.route(trade_envelope())
    duplicate = coordinator.route(order_envelope())
    assert order_result.duplicate is False
    assert trade_result.portfolio_evidence == {"portfolio_version": "portfolio-v1"}
    assert duplicate.duplicate is True
    assert consumer.order_calls == 1
    assert consumer.trade_calls == 1
    assert len(journal.read("factual_event.processing_started")) == 2
    assert len(journal.read("factual_event.processed")) == 2


def test_same_factual_event_id_with_changed_content_fails_closed() -> None:
    consumer = Consumer()
    coordinator = router(
        consumer=consumer,
        repository=InMemoryStateRepository(),
        journal=RuntimeEventJournal(run_id="run-1"),
        clock=Clock(),
    )
    coordinator.route(order_envelope())
    changed_payload = order_event(event="cancelled")
    changed = FactualEventEnvelope(
        event_id="factual-order-1",
        aggregate_id="portfolio-1:BTCUSDT",
        symbol="BTCUSDT",
        timestamp=changed_payload.timestamp,
        kind=FactualEventKind.ORDER,
        payload=changed_payload,
    )
    with pytest.raises(FactualEventIdentityConflict):
        coordinator.route(changed)
    assert consumer.order_calls == 1


def test_processing_failure_restores_consumer_and_requires_recovery() -> None:
    consumer = Consumer()
    consumer.fail_order_once = True
    clock = Clock()
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    coordinator = router(
        consumer=consumer,
        repository=repository,
        journal=RuntimeEventJournal(run_id="run-1", store=event_store),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="order consumer failed"):
        coordinator.route(order_envelope())
    assert consumer.order_calls == 0
    assert coordinator.recovery_required is True
    with pytest.raises(FactualEventRecoveryRequired):
        coordinator.route(trade_envelope())

    recovered = router(
        consumer=consumer,
        repository=repository,
        journal=RuntimeEventJournal(run_id="run-1", store=event_store),
        clock=clock,
    )
    result = recovered.recover()
    assert result is not None
    assert result.event_id == "factual-order-1"
    assert consumer.order_calls == 1
    assert recovered.recovery_required is False


def test_terminal_journal_failure_recovers_without_reapplying_consumer() -> None:
    consumer = Consumer()
    clock = Clock()
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    coordinator = router(
        consumer=consumer,
        repository=repository,
        journal=FailTerminalJournal(run_id="run-1", store=event_store),
        clock=clock,
    )
    with pytest.raises(RuntimeError, match="terminal journal failure"):
        coordinator.route(trade_envelope())
    assert consumer.trade_calls == 1
    assert coordinator.recovery_required is True

    recovered_journal = RuntimeEventJournal(run_id="run-1", store=event_store)
    recovered = router(
        consumer=consumer,
        repository=repository,
        journal=recovered_journal,
        clock=clock,
    )
    result = recovered.recover()
    assert result is not None
    assert consumer.trade_calls == 1
    assert len(recovered_journal.read("factual_event.processed")) == 1
    assert recovered.recovery_required is False


def test_corrupt_routing_state_fails_closed_on_restart() -> None:
    consumer = Consumer()
    repository = InMemoryStateRepository()
    coordinator = router(
        consumer=consumer,
        repository=repository,
        journal=RuntimeEventJournal(run_id="run-1"),
        clock=Clock(),
    )
    coordinator.route(order_envelope())
    state = repository.load(coordinator._state_key)
    assert state is not None
    tampered = dict(state)
    tampered["processed"] = {"factual-order-1": "f" * 64}
    repository.save(coordinator._state_key, tampered)
    with pytest.raises(FactualEventRecoveryRequired, match="integrity"):
        router(
            consumer=consumer,
            repository=repository,
            journal=RuntimeEventJournal(run_id="run-1"),
            clock=Clock(),
        )
