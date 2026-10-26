from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from decimal import Decimal

import pytest

from modular_core import (
    Candle,
    Feature,
    FeatureView,
    MarketView,
    OrderType,
    PositionSide,
    Side,
)
from modular_core.audit import Journal
from modular_core.config import EngineConfig
from modular_core.execution import BrokerSimulator
from modular_core.instruments import InstrumentSpec
from modular_strategy_trade_plan_router import (
    CoreOrderBridgeError,
    DecisionRouter,
    EXECUTABLE_PLAN_FEATURE_KEY,
    TargetIntent,
    TradePlanCoreOrderModule,
    TradeSide,
)

from conftest import NOW, make_entry, make_package, make_protection, make_sizing


def _plan(*, side: TradeSide = TradeSide.LONG):
    entry = make_entry(side=side)
    protection = make_protection(entry)
    sizing = make_sizing(entry, protection)
    result = DecisionRouter().route(
        make_package(entries=(entry,), protections=(protection,), sizings=(sizing,))
    )
    assert result.plan is not None
    return result.plan


def _view(value: object | None, *, timestamp=NOW) -> MarketView:
    candle = Candle(
        timestamp=timestamp - timedelta(minutes=5),
        open=100.0,
        high=101.0,
        low=99.0,
        close=100.0,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    features = ()
    if value is not None:
        features = (
            Feature(
                key=EXECUTABLE_PLAN_FEATURE_KEY,
                source_module="trade_plan_router",
                created_at=timestamp,
                value=value,
                symbol="BTCUSDT",
                timeframe="5m",
                valid_until=timestamp + timedelta(minutes=5),
                schema_version="1",
            ),
        )
    return MarketView(
        base_bars=(candle,),
        timeframe_bars={},
        index=0,
        cash=10_000.0,
        equity=10_000.0,
        position=None,
        pending_orders=(),
        features=FeatureView(features, timestamp),
    )


def test_open_plan_maps_to_one_official_core_bracket_order() -> None:
    plan = _plan()
    module = TradePlanCoreOrderModule()

    decision = module.on_bar(_view(plan.to_mapping()))

    assert len(decision.orders) == 1
    order = decision.orders[0]
    assert order.side is Side.BUY
    assert order.position_side is PositionSide.LONG
    assert order.order_type is OrderType.LIMIT
    assert order.quantity == 10.0
    assert order.limit_price == 100.0
    assert order.trigger_price is None
    assert order.stop_loss == 95.0
    assert order.take_profit == 110.0
    assert order.ttl_bars == 12
    assert order.reduce_only is False
    assert order.client_order_id == f"router:{plan.plan_id}"
    assert order.metadata["router_plan_id"] == plan.plan_id
    assert order.metadata["router_content_hash"] == plan.content_hash
    assert order.metadata["router_configuration_hash"] == plan.configuration_hash


def test_short_plan_preserves_side_and_bracket_geometry() -> None:
    plan = _plan(side=TradeSide.SHORT)

    order = TradePlanCoreOrderModule().on_bar(_view(plan.to_mapping())).orders[0]

    assert order.side is Side.SELL
    assert order.position_side is PositionSide.SHORT
    assert order.stop_loss == 105.0
    assert order.take_profit == 90.0


def test_same_plan_is_emitted_exactly_once_and_state_survives_restore() -> None:
    plan = _plan()
    view = _view(plan.to_mapping())
    first = TradePlanCoreOrderModule()

    assert len(first.on_bar(view).orders) == 1
    duplicate = first.on_bar(view)
    assert duplicate.orders == ()
    assert duplicate.skips == ("trade_plan_already_processed",)

    restored = TradePlanCoreOrderModule()
    restored.restore_state(first.snapshot_state())
    replay = restored.on_bar(view)
    assert replay.orders == ()
    assert replay.skips == ("trade_plan_already_processed",)


def test_tampered_plan_fails_canonical_router_validation() -> None:
    plan = _plan()
    tampered = dict(plan.to_mapping())
    entry = dict(tampered["entry_intent"])
    entry["quantity"] = "999"
    tampered["entry_intent"] = entry

    with pytest.raises(CoreOrderBridgeError, match="canonical Router plan validation failed"):
        TradePlanCoreOrderModule().on_bar(_view(tampered))


def test_missing_plan_is_a_deterministic_skip() -> None:
    decision = TradePlanCoreOrderModule().on_bar(_view(None))
    assert decision.orders == ()
    assert decision.skips == ("no_executable_trade_plan",)


def test_multiple_or_partial_targets_fail_closed() -> None:
    entry = make_entry()
    base = make_protection(entry)
    protection = replace(
        base,
        targets=(
            TargetIntent(Decimal("108"), allocation=Decimal("0.5")),
            TargetIntent(Decimal("112"), allocation=Decimal("0.5")),
        ),
        input_hash="",
    )
    sizing = make_sizing(entry, protection)
    result = DecisionRouter().route(
        make_package(entries=(entry,), protections=(protection,), sizings=(sizing,))
    )
    assert result.plan is not None

    with pytest.raises(CoreOrderBridgeError, match="exactly one Router target"):
        TradePlanCoreOrderModule().on_bar(_view(result.plan.to_mapping()))


def test_unsupported_target_type_fails_closed_after_canonical_validation() -> None:
    entry = make_entry()
    base = make_protection(entry)
    protection = replace(
        base,
        targets=(TargetIntent(Decimal("110"), target_type="market"),),
        input_hash="",
    )
    sizing = make_sizing(entry, protection)
    result = DecisionRouter().route(
        make_package(entries=(entry,), protections=(protection,), sizings=(sizing,))
    )
    assert result.plan is not None

    with pytest.raises(CoreOrderBridgeError, match="supports Router limit target only"):
        TradePlanCoreOrderModule().on_bar(_view(result.plan.to_mapping()))


def test_stale_plan_fails_closed() -> None:
    plan = _plan()

    with pytest.raises(CoreOrderBridgeError, match="Router plan is stale"):
        TradePlanCoreOrderModule().on_bar(_view(plan.to_mapping(), timestamp=plan.expires_at))


def test_same_restored_plan_id_with_different_content_hash_fails_closed() -> None:
    plan = _plan()
    module = TradePlanCoreOrderModule()
    module.restore_state(
        {
            "schema_version": "1",
            "last_plan_id": plan.plan_id,
            "last_content_hash": "0" * 64,
        }
    )

    with pytest.raises(CoreOrderBridgeError, match="same plan_id"):
        module.on_bar(_view(plan.to_mapping()))


def test_open_plan_order_is_accepted_and_filled_by_official_core_broker() -> None:
    plan = _plan()
    module = TradePlanCoreOrderModule()
    order = module.on_bar(_view(plan.to_mapping())).orders[0]
    broker = BrokerSimulator(
        EngineConfig(initial_cash=10_000.0),
        Journal(run_id="router-core-order-bridge"),
        InstrumentSpec(
            symbol="BTCUSDT",
            tick_size=0.1,
            quantity_step=0.001,
            min_quantity=0.001,
            min_notional=5.0,
        ),
    )

    submitted = broker.submit(
        order,
        source_module=module.name,
        created_at=NOW,
        current_index=0,
        mark_price=100.0,
    )
    assert submitted is not None

    broker.process_bar(
        Candle(
            timestamp=NOW + timedelta(minutes=5),
            open=100.0,
            high=101.0,
            low=99.0,
            close=100.0,
            volume=1.0,
            symbol="BTCUSDT",
            timeframe="5m",
        ),
        1,
    )

    position = broker.portfolio.position
    assert position is not None
    assert position.side is PositionSide.LONG
    assert position.quantity == 10.0
    assert position.stop_loss == 95.0
    assert position.take_profit == 110.0
    assert position.setup_id == plan.setup_id
