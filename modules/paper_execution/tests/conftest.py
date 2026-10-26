from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_core import (
    EngineConfig,
    ExecutionConfig,
    Journal,
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
    PlanAction,
    StateVersions,
    VenueMetadataSnapshot,
)

UTC = timezone.utc


class StaticMetadataProvider:
    def __init__(self, metadata: VenueMetadataSnapshot) -> None:
        self.metadata = metadata

    def snapshot(self, instrument: str, timestamp: datetime) -> VenueMetadataSnapshot:
        assert instrument == self.metadata.instrument
        return self.metadata


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


@pytest.fixture
def versions() -> StateVersions:
    return StateVersions(portfolio=3, positions=4, orders=5)


@pytest.fixture
def metadata(now: datetime) -> VenueMetadataSnapshot:
    return VenueMetadataSnapshot(
        venue_id="paper-core",
        instrument="BTCUSDT",
        market_type="linear_futures",
        base_asset="BTC",
        quote_asset="USDT",
        tick_size=Decimal("0.10"),
        quantity_step=Decimal("0.001"),
        min_quantity=Decimal("0.001"),
        max_quantity=Decimal("100"),
        min_notional=Decimal("5"),
        max_notional=Decimal("1000000"),
        contract_multiplier=Decimal("1"),
        permitted_order_types=tuple(OrderType),
        permitted_time_in_force=tuple(TimeInForce),
        post_only_supported=True,
        reduce_only_supported=True,
        metadata_version="binance-usdtm-2026-01-01",
        effective_from=now - timedelta(days=1),
        source_hash="a" * 64,
    )


@pytest.fixture
def entry_intent() -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="entry-1",
        role=IntentRole.ENTRY,
        side=Side.BUY,
        order_type=OrderType.LIMIT,
        quantity=Decimal("0.0129"),
        limit_price=Decimal("99999.97"),
    )


@pytest.fixture
def plan(
    now: datetime,
    versions: StateVersions,
    entry_intent: ExecutionIntent,
) -> ExecutableTradePlan:
    config = PaperExecutionConfig()
    return ExecutableTradePlan.create(
        plan_id="plan-0001",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="binance_usdtm",
        action=PlanAction.OPEN_POSITION,
        intents=(entry_intent,),
        expected_versions=versions,
        metadata_version="binance-usdtm-2026-01-01",
        configuration_hash=config.canonical_hash,
        input_hashes={"decision": "b" * 64},
    )


@pytest.fixture
def broker(metadata: VenueMetadataSnapshot) -> BrokerSimulator:
    config = EngineConfig(
        one_active_order=False,
        execution=ExecutionConfig(max_fill_fraction_per_bar=0.5),
    )
    return BrokerSimulator(
        config,
        Journal(),
        instrument=metadata.to_core_spec(),
    )
