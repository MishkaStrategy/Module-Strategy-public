from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from modular_core import (
    InMemoryEventStore,
    InMemoryStateRepository,
    OrderEvent,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)

from modular_trading_runner import FactualEventRouter
from modular_trading_runner.canonical import canonical_sha256
from modular_trading_runner.errors import FactualEventRecoveryRequired
from modular_trading_runner.journal import RuntimeEventJournal
from modular_trading_runner.routing import FactualEventEnvelope, FactualEventKind
from modular_trading_runner.routing import (
    FactualEventRouter as LegacyFactualEventRouter,
)

NOW = datetime(2026, 8, 2, 12, 0, tzinfo=UTC)
STATE_KEY = "trading-runner-routing:wave2b-routing-v2"


class _Clock:
    def __init__(self, start: datetime = NOW) -> None:
        self.value = start

    def now(self) -> datetime:
        current = self.value
        self.value += timedelta(milliseconds=1)
        return current


class _Consumer:
    def __init__(self, *, fail_once: bool = False) -> None:
        self.order_calls = 0
        self.trade_calls = 0
        self.fail_once = fail_once

    def on_order_event(self, event: OrderEvent) -> None:
        del event
        self.order_calls += 1
        if self.fail_once:
            self.fail_once = False
            raise RuntimeError("injected consumer failure")

    def on_trade(self, trade: object) -> None:
        del trade
        self.trade_calls += 1

    def snapshot_state(self) -> Mapping[str, object]:
        return {
            "order_calls": self.order_calls,
            "trade_calls": self.trade_calls,
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        self.order_calls = int(state["order_calls"])
        self.trade_calls = int(state["trade_calls"])


class _FailOnSaveRepository(InMemoryStateRepository):
    def __init__(self, fail_on: int) -> None:
        super().__init__()
        self.fail_on = fail_on
        self.save_calls = 0
        self.failed = False

    def save(self, key: str, value: Mapping[str, Any]) -> None:
        self.save_calls += 1
        if self.save_calls == self.fail_on and not self.failed:
            self.failed = True
            raise OSError("injected state save failure")
        super().save(key, value)


def _event(suffix: str, *, timestamp: datetime = NOW) -> FactualEventEnvelope:
    snapshot = OrderSnapshot(
        id=f"core-order-{suffix}",
        source_module="paper_execution_venue_reconciliation",
        created_at=timestamp,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=95.0,
        take_profit=110.0,
        tag="",
        setup_id=f"setup-{suffix}",
        client_order_id=f"paper-order-{suffix}",
        status=OrderStatus.FILLED,
        filled_quantity=1.0,
        remaining_quantity=0.0,
        average_fill_price=100.0,
        fees_paid=0.05,
        time_in_force=TimeInForce.GTC,
    )
    order_event = OrderEvent(
        timestamp=timestamp,
        event="filled",
        order=snapshot,
        event_id=f"core-event-{suffix}",
        correlation_id=f"plan-{suffix}",
        causation_id=f"paper-event-{suffix}",
    )
    return FactualEventEnvelope(
        event_id=f"factual-event-{suffix}",
        aggregate_id="portfolio-main:BTCUSDT",
        symbol="BTCUSDT",
        timestamp=timestamp,
        kind=FactualEventKind.ORDER,
        payload=order_event,
        correlation_id=order_event.correlation_id,
        causation_id=order_event.causation_id,
    )


def _router(
    *,
    consumer: _Consumer,
    repository: InMemoryStateRepository,
    event_store: InMemoryEventStore,
    clock: _Clock | None = None,
    legacy: bool = False,
) -> FactualEventRouter | LegacyFactualEventRouter:
    router_type = LegacyFactualEventRouter if legacy else FactualEventRouter
    return router_type(
        run_id="wave2b-routing-v2",
        config_hash="a" * 64,
        module_graph_hash="b" * 64,
        consumer=consumer,
        state_repository=repository,
        journal=RuntimeEventJournal(
            run_id="wave2b-routing-v2",
            store=event_store,
        ),
        clock=clock or _Clock(),
        queue_capacity=32,
    )


def test_v2_checkpoint_is_bounded_and_restart_suppresses_duplicate() -> None:
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    consumer = _Consumer()
    event = _event("one")

    result = _router(
        consumer=consumer,
        repository=repository,
        event_store=event_store,
    ).route(event)
    assert result.duplicate is False
    snapshot = repository.load(STATE_KEY)
    assert snapshot is not None
    assert snapshot["schema_version"] == "2"
    assert snapshot["processed_count"] == 1
    assert "processed" not in snapshot

    restarted_consumer = _Consumer()
    restarted = _router(
        consumer=restarted_consumer,
        repository=repository,
        event_store=event_store,
    )
    duplicate = restarted.route(event)
    assert duplicate.duplicate is True
    assert restarted_consumer.order_calls == 0


def test_v2_loads_exact_legacy_state_and_migrates_on_next_event() -> None:
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    first = _event("legacy")
    _router(
        consumer=_Consumer(),
        repository=repository,
        event_store=event_store,
        legacy=True,
    ).route(first)
    legacy_snapshot = repository.load(STATE_KEY)
    assert legacy_snapshot is not None
    assert legacy_snapshot["schema_version"] == "1"

    restarted = _router(
        consumer=_Consumer(),
        repository=repository,
        event_store=event_store,
        clock=_Clock(NOW + timedelta(seconds=1)),
    )
    assert restarted.route(first).duplicate is True
    restarted.route(_event("v2", timestamp=NOW + timedelta(seconds=1)))
    migrated = repository.load(STATE_KEY)
    assert migrated is not None
    assert migrated["schema_version"] == "2"
    assert migrated["processed_count"] == 2
    assert "processed" not in migrated


def test_v2_rejects_processed_count_that_disagrees_with_journal() -> None:
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    _router(
        consumer=_Consumer(),
        repository=repository,
        event_store=event_store,
    ).route(_event("count"))
    snapshot = repository.load(STATE_KEY)
    assert snapshot is not None
    tampered = dict(snapshot)
    tampered.pop("integrity_hash")
    tampered["processed_count"] = 0
    tampered["integrity_hash"] = canonical_sha256(tampered)
    repository.save(STATE_KEY, tampered)

    with pytest.raises(FactualEventRecoveryRequired, match="processed_count"):
        _router(
            consumer=_Consumer(),
            repository=repository,
            event_store=event_store,
        )


def test_v2_recovers_terminal_journal_after_final_checkpoint_failure() -> None:
    repository = _FailOnSaveRepository(fail_on=3)
    event_store = InMemoryEventStore()
    consumer = _Consumer()
    event = _event("terminal")
    with pytest.raises(OSError, match="injected state save failure"):
        _router(
            consumer=consumer,
            repository=repository,
            event_store=event_store,
        ).route(event)
    assert consumer.order_calls == 1

    restarted_consumer = _Consumer()
    restarted = _router(
        consumer=restarted_consumer,
        repository=repository,
        event_store=event_store,
    )
    assert restarted.recovery_required is True
    recovered = restarted.recover()
    assert recovered is not None
    assert recovered.event_id == event.event_id
    assert restarted_consumer.order_calls == 0
    assert restarted.route(event).duplicate is True


def test_v2_accepts_same_hash_repeated_start_after_processing_retry() -> None:
    repository = InMemoryStateRepository()
    event_store = InMemoryEventStore()
    event = _event("retry")
    with pytest.raises(RuntimeError, match="injected consumer failure"):
        _router(
            consumer=_Consumer(fail_once=True),
            repository=repository,
            event_store=event_store,
        ).route(event)

    retry_consumer = _Consumer()
    retry = _router(
        consumer=retry_consumer,
        repository=repository,
        event_store=event_store,
    )
    recovered = retry.recover()
    assert recovered is not None
    assert retry_consumer.order_calls == 1

    final_restart = _router(
        consumer=_Consumer(),
        repository=repository,
        event_store=event_store,
    )
    assert final_restart.route(event).duplicate is True
