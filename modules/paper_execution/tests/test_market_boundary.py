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
    PlanAction,
    ReconciliationBlocked,
    VenueMarkSnapshot,
    markets_compatible,
)


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _bracket_plan(plan, versions, now) -> ExecutableTradePlan:
    entry = plan.intents[0]
    return ExecutableTradePlan.create(
        plan_id="mark-provider-required",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id=plan.portfolio_id,
        instrument=plan.instrument,
        market=plan.market,
        action=PlanAction.OPEN_POSITION,
        intents=(
            entry,
            ExecutionIntent(
                intent_id="required-stop",
                role=IntentRole.STOP,
                side=Side.SELL,
                order_type=OrderType.STOP_MARKET,
                quantity=entry.quantity,
                trigger_price=Decimal("99000"),
                reduce_only=True,
                oco_group_id="required-bracket",
            ),
        ),
        expected_versions=versions,
        metadata_version=plan.metadata_version,
        configuration_hash=PaperExecutionConfig().canonical_hash,
        input_hashes={"decision": "f" * 64},
    )


def test_market_compatibility_is_explicit_and_fail_closed() -> None:
    assert markets_compatible("binance_usdtm", "linear_futures")
    assert markets_compatible("linear_futures", "linear_futures")
    assert markets_compatible("binance_coinm", "inverse_futures")
    assert markets_compatible("binance_spot", "spot")
    assert not markets_compatible("binance_spot", "linear_futures")
    assert not markets_compatible("unknown_market", "linear_futures")
    assert not markets_compatible("binance_usdtm", "unknown_venue_market")
    assert not markets_compatible("", "linear_futures")


def test_mark_snapshot_requires_exact_scope_timestamp(now) -> None:
    snapshot = VenueMarkSnapshot(
        instrument="BTCUSDT",
        price=Decimal("100000"),
        observed_at=now,
        source="test",
    )
    snapshot.validate_for(instrument="BTCUSDT", timestamp=now)

    with pytest.raises(ValueError, match="instrument"):
        snapshot.validate_for(instrument="ETHUSDT", timestamp=now)
    with pytest.raises(ValueError, match="stale or from a future"):
        snapshot.validate_for(
            instrument="BTCUSDT",
            timestamp=now + timedelta(microseconds=1),
        )
    future = VenueMarkSnapshot(
        instrument="BTCUSDT",
        price=Decimal("100000"),
        observed_at=now + timedelta(microseconds=1),
        source="test",
    )
    with pytest.raises(ValueError, match="stale or from a future"):
        future.validate_for(instrument="BTCUSDT", timestamp=now)


def test_deferred_protection_without_mark_provider_fails_closed(
    plan,
    versions,
    now,
    metadata,
) -> None:
    broker = BrokerSimulator(
        EngineConfig(
            one_active_order=False,
            execution=ExecutionConfig(max_fill_fraction_per_bar=1.0),
        ),
        Journal(),
        instrument=metadata.to_core_spec(),
    )
    service = PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="missing-mark-provider",
    )
    result = service.accept_plan(
        _bracket_plan(plan, versions, now),
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    assert result.accepted

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
    with pytest.raises(
        ReconciliationBlocked,
        match="requires a fresh venue mark provider",
    ):
        for event in tuple(broker.journal.orders):
            service.ingest_order_event(event)
