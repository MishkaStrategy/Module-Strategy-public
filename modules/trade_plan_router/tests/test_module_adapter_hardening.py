from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from modular_core import FeatureStore, OrderStatus, OrderType, PositionSide, Side

from modular_strategy_trade_plan_router import DecisionRouter, TradePlanRouterModule
from modular_strategy_trade_plan_router.enums import PlanVerdict, ReasonCode, TradeSide
from modular_strategy_trade_plan_router.errors import PlanInvariantError, RouterInputError
from modular_strategy_trade_plan_router.module import (
    CONFLICT_TRACE_FEATURE_KEY,
    ENTRY_PLAN_FEATURE_KEY,
    EXECUTABLE_PLAN_FEATURE_KEY,
    LEGACY_ENTRY_PLAN_FEATURE_KEY,
    PORTFOLIO_SNAPSHOT_FEATURE_KEY,
    PROTECTION_PLAN_FEATURE_KEY,
    REJECTED_PLAN_FEATURE_KEY,
    RISK_DECISION_FEATURE_KEY,
    ROUTING_DECISION_FEATURE_KEY,
    SCALING_APPROVED_FEATURE_KEY,
    _core_datetime,
    _core_decimal,
    _core_text,
    _order_side,
    _order_status,
    _order_type,
    _position_side,
)

from conftest import NOW, make_package


class FakeFeatures:
    def __init__(self, rows: dict[str, tuple[object, ...]] | None = None) -> None:
        self.rows = rows or {}

    def get_all(self, key: str, **_filters: object) -> tuple[object, ...]:
        return self.rows.get(key, ())

    def latest(self, key: str, **_filters: object) -> object | None:
        rows = self.rows.get(key, ())
        return rows[-1] if rows else None


def current() -> SimpleNamespace:
    return SimpleNamespace(
        symbol="BTCUSDT",
        timeframe="5m",
        close_time=NOW,
    )


def view(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "current": current(),
        "features": FakeFeatures(),
        "equity": Decimal("10000"),
        "cash": Decimal("9000"),
        "position": None,
        "pending_orders": (),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def feature(key: str, value: object, *, index: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        key=key,
        value=value,
        schema_version="1",
        created_at=NOW,
        valid_until=NOW + timedelta(minutes=5),
        feature_id=f"feature-{index}",
        source_module="fixture",
    )


def test_core_boundary_helpers_accept_valid_values() -> None:
    assert _core_decimal("1.25", "value") == Decimal("1.25")
    assert _core_decimal(2, "value") == Decimal("2")
    assert _core_text("BTCUSDT", "symbol") == "BTCUSDT"
    assert _core_datetime(NOW, "as_of") == NOW
    assert _position_side(PositionSide.LONG) is TradeSide.LONG
    assert _position_side(PositionSide.SHORT) is TradeSide.SHORT
    assert _order_side(Side.BUY) is TradeSide.LONG
    assert _order_side(Side.SELL) is TradeSide.SHORT
    assert _order_status(OrderStatus.PENDING) is OrderStatus.PENDING
    assert _order_type(OrderType.LIMIT) == "limit"


@pytest.mark.parametrize(
    ("call", "message"),
    (
        (lambda: _core_decimal(True, "value"), "must be numeric"),
        (lambda: _core_decimal("not-a-number", "value"), "must be numeric"),
        (lambda: _core_decimal("NaN", "value"), "must be finite"),
        (lambda: _core_text("", "value"), "non-blank"),
        (lambda: _core_text(" value", "value"), "non-blank"),
        (lambda: _core_text("e\u0301", "value"), "NFC"),
        (lambda: _core_text("x" * 513, "value"), "maximum length"),
        (lambda: _core_text("line\nbreak", "value"), "control"),
        (lambda: _core_datetime(datetime(2026, 1, 1), "value"), "timezone-aware"),
        (lambda: _position_side("long"), "PositionSide"),
        (lambda: _order_side("buy"), "Side.BUY"),
        (lambda: _order_status("pending"), "OrderStatus"),
        (lambda: _order_type("limit"), "OrderType"),
    ),
)
def test_core_boundary_helpers_reject_invalid_values(call, message: str) -> None:
    with pytest.raises(RouterInputError, match=message):
        call()


def test_stateless_state_contract() -> None:
    module = TradePlanRouterModule()
    assert dict(module.snapshot_state()) == {}
    module.restore_state({})

    with pytest.raises(ValueError, match="only empty state"):
        module.restore_state({"unexpected": True})
    with pytest.raises(ValueError, match="empty mapping"):
        module.restore_state([])  # type: ignore[arg-type]
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="invalid Router state"):
        module.restore_state(cyclic)


def test_fallback_portfolio_maps_position_and_pending_orders() -> None:
    module = TradePlanRouterModule()
    position = SimpleNamespace(
        id="position-1",
        side=PositionSide.LONG,
        quantity=Decimal("2"),
        entry_price=Decimal("100"),
        setup_id="setup-a",
        entry_time=NOW - timedelta(minutes=10),
    )
    limit_order = SimpleNamespace(
        id="order-limit",
        status=OrderStatus.PENDING,
        side=Side.BUY,
        setup_id="setup-a",
        created_at=NOW - timedelta(minutes=1),
        good_till_time=NOW + timedelta(minutes=5),
        quantity=Decimal("3"),
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("2"),
        order_type=OrderType.LIMIT,
        limit_price=None,
        trigger_price=None,
        price=Decimal("99"),
        reduce_only=False,
        post_only=True,
    )
    stop_order = SimpleNamespace(
        id="order-stop",
        status=OrderStatus.PARTIALLY_FILLED,
        side=Side.SELL,
        setup_id=None,
        created_at=NOW - timedelta(minutes=2),
        good_till_time=None,
        quantity=Decimal("4"),
        filled_quantity=Decimal("1"),
        remaining_quantity=Decimal("0"),
        order_type=OrderType.STOP_MARKET,
        limit_price=None,
        trigger_price=None,
        price=Decimal("95"),
        reduce_only=True,
        post_only=False,
    )
    terminal = SimpleNamespace(status=OrderStatus.FILLED)

    snapshot = module._fallback_portfolio(
        view(
            position=position,
            pending_orders=(limit_order, stop_order, terminal),
        ),
        NOW,
    )

    assert snapshot.portfolio_id == "default"
    assert snapshot.available_risk_budget == Decimal("300")
    assert len(snapshot.positions) == 1
    assert snapshot.positions[0].side is TradeSide.LONG
    assert len(snapshot.pending_orders) == 2
    assert snapshot.pending_orders[0].price == Decimal("99")
    assert snapshot.pending_orders[0].post_only is True
    assert snapshot.pending_orders[1].stop_price == Decimal("95")
    assert snapshot.pending_orders[1].reduce_only is True


def _order(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "id": "order-a",
        "status": OrderStatus.PENDING,
        "side": Side.BUY,
        "setup_id": None,
        "created_at": NOW,
        "good_till_time": None,
        "quantity": Decimal("2"),
        "filled_quantity": Decimal("1"),
        "remaining_quantity": Decimal("1"),
        "order_type": OrderType.LIMIT,
        "limit_price": Decimal("100"),
        "trigger_price": None,
        "price": None,
        "reduce_only": False,
        "post_only": False,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("order", "message"),
    (
        (_order(filled_quantity=Decimal("3")), "cannot exceed quantity"),
        (_order(remaining_quantity=Decimal("0.5")), "remaining_quantity conflicts"),
        (
            _order(
                quantity=Decimal("1"),
                filled_quantity=Decimal("1"),
                remaining_quantity=Decimal("0"),
            ),
            "positive remaining quantity",
        ),
    ),
)
def test_fallback_portfolio_rejects_inconsistent_pending_orders(
    order: SimpleNamespace,
    message: str,
) -> None:
    with pytest.raises(RouterInputError, match=message):
        TradePlanRouterModule()._fallback_portfolio(
            view(pending_orders=(order,)),
            NOW,
        )


def test_portfolio_selection_uses_feature_or_required_policy() -> None:
    package = make_package()
    snapshot_feature = feature(PORTFOLIO_SNAPSHOT_FEATURE_KEY, {"fixture": True})
    module = TradePlanRouterModule(require_portfolio_snapshot=True)
    module._normalizer = SimpleNamespace(
        normalize_portfolio=lambda _value, **_kwargs: package.portfolio
    )
    selected = module._portfolio(
        view(features=FakeFeatures({PORTFOLIO_SNAPSHOT_FEATURE_KEY: (snapshot_feature,)})),
        NOW,
    )
    assert selected.portfolio_id == package.portfolio.portfolio_id

    with pytest.raises(RouterInputError, match="portfolio.snapshot is required") as error:
        module._portfolio(view(), NOW)
    assert error.value.reason is ReasonCode.PORTFOLIO_STATE_STALE


def test_feature_extractors_use_both_entry_keys_and_enforce_limits() -> None:
    module = TradePlanRouterModule(max_entry_candidates=2)
    rows = {
        ENTRY_PLAN_FEATURE_KEY: (feature(ENTRY_PLAN_FEATURE_KEY, "entry-a", index=1),),
        LEGACY_ENTRY_PLAN_FEATURE_KEY: (
            feature(LEGACY_ENTRY_PLAN_FEATURE_KEY, "entry-b", index=2),
        ),
        PROTECTION_PLAN_FEATURE_KEY: (feature(PROTECTION_PLAN_FEATURE_KEY, "protection", index=3),),
        RISK_DECISION_FEATURE_KEY: (feature(RISK_DECISION_FEATURE_KEY, "sizing", index=4),),
        SCALING_APPROVED_FEATURE_KEY: (feature(SCALING_APPROVED_FEATURE_KEY, "scaling", index=5),),
    }
    module._normalizer = SimpleNamespace(
        normalize_entry=lambda value, **_kwargs: value,
        normalize_protection=lambda value, **_kwargs: value,
        normalize_sizing=lambda value, **_kwargs: value,
        normalize_scaling=lambda value, **_kwargs: value,
    )
    market_view = view(features=FakeFeatures(rows))
    assert module._entries(market_view, "portfolio-a") == ("entry-a", "entry-b")
    assert module._protections(market_view) == ("protection",)
    assert module._sizings(market_view) == ("sizing",)
    assert module._scalings(market_view) == ("scaling",)

    limited = TradePlanRouterModule(max_entry_candidates=1)
    limited._normalizer = module._normalizer
    with pytest.raises(RouterInputError, match="exceeds configured maximum") as error:
        limited._entries(market_view, "portfolio-a")
    assert error.value.reason is ReasonCode.INPUT_LIMIT_EXCEEDED


@pytest.mark.parametrize("method_name", ("_entries", "_protections", "_sizings", "_scalings"))
def test_feature_extractors_reject_missing_feature_view(method_name: str) -> None:
    module = TradePlanRouterModule()
    method = getattr(module, method_name)
    arguments = (
        (view(features=None), "portfolio-a")
        if method_name == "_entries"
        else (view(features=None),)
    )
    with pytest.raises(RouterInputError, match="features is unavailable"):
        method(*arguments)


def test_on_bar_without_features_publishes_typed_rejection() -> None:
    decision = TradePlanRouterModule().on_bar(view(features=None))
    keys = {item.key for item in decision.features}
    assert keys == {
        ROUTING_DECISION_FEATURE_KEY,
        CONFLICT_TRACE_FEATURE_KEY,
        REJECTED_PLAN_FEATURE_KEY,
    }
    assert all(item.valid_until == NOW for item in decision.features)
    store = FeatureStore()
    for item in decision.features:
        store.publish(item)
    assert set(store.snapshot(NOW).keys()) == keys
    expired = store.expire(NOW + timedelta(microseconds=1))
    assert {item.key for item in expired} == keys
    assert store.count == 0
    assert ReasonCode.MISSING_ENTRY_DECISION.value in decision.skips
    assert decision.diagnostics["verdict"] == PlanVerdict.REJECTED.value


def test_rejected_router_features_remain_bounded_across_bars() -> None:
    module = TradePlanRouterModule()
    store = FeatureStore()

    for index in range(256):
        timestamp = NOW + timedelta(minutes=5 * index)
        store.expire(timestamp)
        market_view = view(features=None)
        market_view.current.close_time = timestamp
        decision = module.on_bar(market_view)
        for item in decision.features:
            store.publish(item)
        assert store.count == 3


def test_on_bar_accepted_path_publishes_executable_plan(monkeypatch) -> None:
    package = make_package()
    module = TradePlanRouterModule()
    monkeypatch.setattr(module, "_portfolio", lambda _view, _as_of: package.portfolio)
    monkeypatch.setattr(module, "_entries", lambda _view, _portfolio_id: package.entries)
    monkeypatch.setattr(module, "_protections", lambda _view: package.protections)
    monkeypatch.setattr(module, "_sizings", lambda _view: package.sizings)
    monkeypatch.setattr(module, "_scalings", lambda _view: package.scalings)

    decision = module.on_bar(view(features=FakeFeatures()))
    keys = {item.key for item in decision.features}
    assert keys == {
        ROUTING_DECISION_FEATURE_KEY,
        CONFLICT_TRACE_FEATURE_KEY,
        EXECUTABLE_PLAN_FEATURE_KEY,
    }
    assert decision.skips == ()
    assert decision.diagnostics["verdict"] == PlanVerdict.ACCEPTED.value
    terminal = next(item for item in decision.features if item.key == EXECUTABLE_PLAN_FEATURE_KEY)
    transient = tuple(item for item in decision.features if item.key != EXECUTABLE_PLAN_FEATURE_KEY)
    assert all(item.valid_until == NOW for item in transient)
    assert terminal.valid_until is not None
    assert terminal.valid_until > NOW

    store = FeatureStore()
    for item in decision.features:
        store.publish(item)
    expired = store.expire(NOW + timedelta(microseconds=1))
    assert {item.key for item in expired} == {
        ROUTING_DECISION_FEATURE_KEY,
        CONFLICT_TRACE_FEATURE_KEY,
    }
    assert (
        store.snapshot(NOW + timedelta(microseconds=1)).latest(EXECUTABLE_PLAN_FEATURE_KEY)
        is not None
    )


def test_decision_rejects_impossible_accepted_result_without_plan() -> None:
    package = make_package()
    result = DecisionRouter().route(package)
    assert result.plan is not None
    object.__setattr__(result, "plan", None)
    with pytest.raises(PlanInvariantError, match="missing executable plan"):
        TradePlanRouterModule()._decision(result, view())
