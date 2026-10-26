from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from modular_core import (
    Candle,
    EngineConfig,
    ExecutionConfig,
    Journal,
    OrderSnapshot,
    OrderStatus,
    OrderType,
    Side,
    TimeInForce,
)
from modular_core.execution import BrokerSimulator

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PaperReasonCode,
    PlanAction,
    ReconciliationBlocked,
    ReconciliationMode,
    VenueMarkSnapshot,
)
from paper_execution.snapshot_identity import build_snapshot_tag


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _broker(metadata, *, fill_fraction: float = 1.0) -> BrokerSimulator:
    return BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(
                max_fill_fraction_per_bar=fill_fraction,
            ),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )


def _coordinator(venue, metadata, *, reconciliation_mode=None):
    config = PaperExecutionConfig(
        reconciliation_mode=(
            reconciliation_mode
            if reconciliation_mode is not None
            else ReconciliationMode.STRICT_BLOCK
        ),
    )
    return PaperExecutionCoordinator(
        config=config,
        venue=venue,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="audit-p0-regressions",
    )


def _market_plan(plan, versions, now, *, market: str) -> ExecutableTradePlan:
    return ExecutableTradePlan.create(
        plan_id="plan-market-mismatch",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=market,
        action=plan.action,
        intents=plan.intents,
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=PaperExecutionConfig().canonical_hash,
        input_hashes={"decision": "d" * 64},
    )


def _bracket_plan(plan, versions, now) -> ExecutableTradePlan:
    entry = plan.intents[0]
    stop = ExecutionIntent(
        intent_id="stop-audit",
        role=IntentRole.STOP,
        side=Side.SELL,
        order_type=OrderType.STOP_MARKET,
        quantity=entry.quantity,
        trigger_price=Decimal("99000"),
        reduce_only=True,
        oco_group_id="audit-bracket",
    )
    target = ExecutionIntent(
        intent_id="target-audit",
        role=IntentRole.TARGET,
        side=Side.SELL,
        order_type=OrderType.TAKE_PROFIT_MARKET,
        quantity=entry.quantity,
        trigger_price=Decimal("110000"),
        reduce_only=True,
        oco_group_id="audit-bracket",
    )
    return ExecutableTradePlan.create(
        plan_id="plan-fresh-mark",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=(entry, stop, target),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=PaperExecutionConfig().canonical_hash,
        input_hashes={"decision": "e" * 64},
    )


def _fill_bar(now) -> Candle:
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


class FreshMarkVenue:
    def __init__(
        self,
        broker: BrokerSimulator,
        *,
        mark_price: Decimal,
        mark_timestamp: datetime,
    ) -> None:
        self.broker = broker
        self.mark_price = mark_price
        self.mark_timestamp = mark_timestamp
        self.mark_snapshot_calls: list[tuple[str, datetime]] = []
        self.position_update_marks: list[float] = []

    def submit(self, request, **kwargs):
        return self.broker.submit(request, **kwargs)

    def cancel(self, request, **kwargs):
        return self.broker.cancel(request, **kwargs)

    def pending_snapshots(self):
        return self.broker.pending_snapshots()

    def process_bar(self, bar, index):
        return self.broker.process_bar(bar, index)

    def mark_snapshot(
        self,
        instrument: str,
        timestamp: datetime,
    ) -> VenueMarkSnapshot:
        self.mark_snapshot_calls.append((instrument, timestamp))
        return VenueMarkSnapshot(
            instrument=instrument,
            price=self.mark_price,
            observed_at=self.mark_timestamp,
            source="audit-test",
        )

    def update_position(self, request, **kwargs):
        self.position_update_marks.append(kwargs["mark_price"])
        return self.broker.update_position(request, **kwargs)


class SnapshotVenue:
    def __init__(self, snapshots) -> None:
        self.snapshots = tuple(snapshots)

    def pending_snapshots(self):
        return self.snapshots

    def submit(self, request, **kwargs):
        raise AssertionError("submit must not be called during reconciliation")

    def cancel(self, request, **kwargs):
        raise AssertionError("cancel must not be called during reconciliation")

    def process_bar(self, bar, index):
        raise AssertionError("process_bar is not part of reconciliation")

    def update_position(self, request, **kwargs):
        raise AssertionError("position repair is forbidden")


def _owned_snapshot(*, portfolio_id: str) -> OrderSnapshot:
    return OrderSnapshot(
        id="core-audit-orphan",
        source_module="paper_execution_venue_reconciliation",
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        stop_loss=None,
        take_profit=None,
        tag=build_snapshot_tag(
            portfolio_id=portfolio_id,
            instrument="BTCUSDT",
            role=IntentRole.ENTRY,
        ),
        setup_id=None,
        client_order_id="pevr-entry-0-auditproof01",
        status=OrderStatus.PENDING,
        filled_quantity=0.0,
        remaining_quantity=1.0,
        average_fill_price=None,
        fees_paid=0.0,
        time_in_force=TimeInForce.GTC,
    )


def test_plan_market_mismatch_is_rejected_before_submission(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata)
    service = _coordinator(broker, metadata)
    mismatched = _market_plan(
        plan,
        versions,
        now,
        market="binance_spot",
    )

    result = service.accept_plan(
        mismatched,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert not result.accepted
    assert result.reason_code is PaperReasonCode.INVALID_PLAN
    assert broker.orders == []


def test_deferred_protection_uses_fresh_venue_mark_snapshot(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = _broker(metadata)
    bar = _fill_bar(now)
    venue = FreshMarkVenue(
        broker,
        mark_price=Decimal("98000"),
        mark_timestamp=bar.timestamp,
    )
    service = _coordinator(venue, metadata)
    service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    broker.process_bar(bar, 1)
    with pytest.raises(
        ReconciliationBlocked,
        match="rejected deferred protection",
    ):
        for event in tuple(broker.journal.orders):
            service.ingest_order_event(event)

    assert venue.mark_snapshot_calls == [("BTCUSDT", bar.timestamp)]
    assert venue.position_update_marks == [98000.0]


def test_safe_auto_repair_reconstructs_unique_owned_orphan(
    metadata,
    now,
) -> None:
    snapshot = _owned_snapshot(portfolio_id="portfolio-main")
    service = _coordinator(
        SnapshotVenue((snapshot,)),
        metadata,
        reconciliation_mode=ReconciliationMode.SAFE_AUTO_REPAIR,
    )

    report = service.reconcile(
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        timestamp=now,
    )

    assert report.findings == ()
    assert not report.blocked
    assert service.orders[snapshot.client_order_id].portfolio_id == "portfolio-main"


def test_orphan_repair_requires_matching_portfolio_evidence(metadata) -> None:
    snapshot = _owned_snapshot(portfolio_id="portfolio-other")

    aggregate = PaperExecutionCoordinator._aggregate_from_owned_snapshot(
        snapshot=snapshot,
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
    )

    assert aggregate is None
