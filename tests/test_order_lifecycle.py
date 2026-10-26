from modular_core import (
    BacktestEngine,
    CancelRequest,
    EngineConfig,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    Side,
    StrategyModule,
)


class ExpiringOrder(StrategyModule):
    name = "expiring"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.LIMIT,
                        quantity=1,
                        price=90,
                        ttl_bars=1,
                        client_order_id="setup-1",
                    ),
                )
            )
        return ModuleDecision()


def test_order_expires_before_later_touch(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99, 100), (90, 91, 80, 90)])
    result = BacktestEngine(ExpiringOrder()).run(bars)
    assert not result.journal.fills
    assert any(event.event == "expired" for event in result.journal.orders)


class SubmitThenCancel(StrategyModule):
    name = "canceller"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.LIMIT,
                        quantity=1,
                        price=90,
                        client_order_id="cancel-me",
                    ),
                )
            )
        if view.index == 1:
            return ModuleDecision(cancellations=(CancelRequest(client_order_id="cancel-me"),))
        return ModuleDecision()


def test_module_can_cancel_pending_order(bars_factory):
    bars = bars_factory([(100, 101, 99, 100), (100, 101, 99, 100), (90, 91, 80, 90)])
    result = BacktestEngine(SubmitThenCancel()).run(bars)
    assert not result.journal.fills
    assert any(event.event == "cancelled" for event in result.journal.orders)


class DuplicateClientIds(StrategyModule):
    name = "duplicate_ids"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.LIMIT,
                        quantity=1,
                        price=90,
                        client_order_id="same",
                    ),
                    OrderRequest(
                        Side.BUY,
                        OrderType.LIMIT,
                        quantity=1,
                        price=89,
                        client_order_id="same",
                    ),
                )
            )
        return ModuleDecision()


def test_duplicate_client_order_id_is_rejected(bars_factory):
    config = EngineConfig(one_active_order=False)
    result = BacktestEngine(DuplicateClientIds(), config).run(bars_factory([100, 101]))
    reasons = [row["reason"] for row in result.journal.skips]
    assert "duplicate_client_order_id" in reasons
    submitted = [event for event in result.journal.orders if event.event == "submitted"]
    assert len(submitted) == 1


class TwoOrders(StrategyModule):
    name = "two_orders"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(Side.BUY, OrderType.LIMIT, quantity=1, price=90),
                    OrderRequest(Side.BUY, OrderType.LIMIT, quantity=1, price=89),
                )
            )
        return ModuleDecision()


def test_order_ids_are_deterministic(bars_factory):
    config = EngineConfig(one_active_order=False)
    result = BacktestEngine(TwoOrders(), config).run(bars_factory([100, 101]))
    ids = [event.order.id for event in result.journal.orders if event.event == "submitted"]
    assert ids == ["ord-00000001", "ord-00000002"]
