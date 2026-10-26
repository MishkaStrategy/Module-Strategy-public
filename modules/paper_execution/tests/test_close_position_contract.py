from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from modular_core import Candle, OrderRequest, OrderType, Side, TimeInForce

from paper_execution import (
    ExecutableTradePlan,
    ExecutionIntent,
    IntentRole,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    PlanAction,
    VenueRuleEngine,
)


class StaticMetadataProvider:
    def __init__(self, metadata) -> None:
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _full_close_intent() -> ExecutionIntent:
    return ExecutionIntent(
        intent_id="close-full",
        role=IntentRole.ENTRY,
        side=Side.SELL,
        order_type=OrderType.MARKET,
        time_in_force=TimeInForce.FOK,
        reduce_only=True,
        close_position=True,
    )


def test_full_close_intent_is_quantityless_and_normalizes_without_inventing_size(
    metadata, now
) -> None:
    intent = _full_close_intent()

    normalized = VenueRuleEngine().normalize(
        intent=intent,
        metadata=metadata,
        client_order_id="pevr-close-full-1234567890",
        reference_price=Decimal("100000"),
        event_time=now,
    )

    assert intent.quantity is None
    assert normalized.request.quantity is None
    assert normalized.request.reduce_only
    assert normalized.request.close_position
    assert normalized.audit.requested_quantity is None
    assert normalized.audit.normalized_quantity is None
    assert normalized.audit.notional is None


def test_full_close_rejects_explicit_quantity() -> None:
    with pytest.raises(ValueError, match="close_position cannot define quantity"):
        ExecutionIntent(
            intent_id="close-explicit",
            role=IntentRole.ENTRY,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            quantity=Decimal("0.01"),
            reduce_only=True,
            close_position=True,
        )


def test_full_close_requires_reduce_only() -> None:
    with pytest.raises(ValueError, match="close_position requires reduce_only"):
        ExecutionIntent(
            intent_id="close-not-reduce-only",
            role=IntentRole.ENTRY,
            side=Side.SELL,
            order_type=OrderType.MARKET,
            close_position=True,
        )


def test_ordinary_order_still_requires_quantity() -> None:
    with pytest.raises(ValueError, match="order intent requires quantity"):
        ExecutionIntent(
            intent_id="ordinary-without-quantity",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
        )


def test_quantityless_full_close_flattens_existing_core_paper_position(
    broker,
    metadata,
    now,
    versions,
) -> None:
    entry = broker.submit(
        OrderRequest(
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=0.012,
            time_in_force=TimeInForce.FOK,
            client_order_id="seed-entry",
        ),
        source_module="test_seed",
        created_at=now - timedelta(minutes=10),
        current_index=0,
        mark_price=100000.0,
    )
    assert entry is not None
    broker.process_bar(
        Candle(
            timestamp=now - timedelta(minutes=5),
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
    assert broker.portfolio.position is not None
    position_quantity = broker.portfolio.position.quantity
    assert position_quantity > 0

    config = PaperExecutionConfig()
    service = PaperExecutionCoordinator(
        config=config,
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id="close-position-contract",
    )
    close_plan = ExecutableTradePlan.create(
        plan_id="plan-close-full",
        created_at=now - timedelta(seconds=1),
        valid_from=now - timedelta(seconds=1),
        expires_at=now + timedelta(minutes=5),
        portfolio_id="portfolio-main",
        instrument="BTCUSDT",
        market="binance_usdtm",
        action=PlanAction.CLOSE_POSITION,
        intents=(_full_close_intent(),),
        expected_versions=versions,
        metadata_version=metadata.metadata_version,
        configuration_hash=config.canonical_hash,
        input_hashes={"decision": "c" * 64},
    )

    result = service.accept_plan(
        close_plan,
        now=now,
        current_index=1,
        mark_price=Decimal("100000"),
        versions=versions,
    )

    assert result.accepted
    assert len(result.client_order_ids) == 1
    client_order_id = result.client_order_ids[0]
    close_order = broker.pending_snapshots()[0]
    assert close_order.client_order_id == client_order_id
    assert close_order.reduce_only
    assert close_order.quantity == pytest.approx(position_quantity)
    assert service.orders[client_order_id].requested_quantity == Decimal(str(position_quantity))

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
        2,
    )

    assert broker.portfolio.position is None
