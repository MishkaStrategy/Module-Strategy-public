from datetime import UTC

import pytest

from modular_core import (
    BacktestEngine,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    SetupEvent,
    SetupRegistry,
    SetupStatus,
    SetupTransitionError,
    Side,
    StrategyModule,
)


class SetupTrade(StrategyModule):
    name = "setup_trade"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                setups=(SetupEvent(self.name, "s1", SetupStatus.CONFIRMED),),
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.MARKET,
                        quantity=1,
                        stop_loss=95,
                        take_profit=102,
                        setup_id="s1",
                    ),
                ),
            )
        return ModuleDecision()


def test_setup_order_fill_close_are_linked_in_registry_and_event_stream(bars_factory):
    result = BacktestEngine(SetupTrade()).run(
        bars_factory([(100, 101, 99, 100), (100, 101, 99, 100), (100, 103, 99, 102)])
    )
    record = result.journal.setup_records[0]
    assert record.status is SetupStatus.CLOSED
    assert record.order_ids == ("ord-00000001",)
    assert record.position_id == "pos-00000001"
    submitted = next(
        event for event in result.journal.events if event.event_type == "order.submitted"
    )
    confirmed = next(
        event for event in result.journal.events if event.event_type == "setup.confirmed"
    )
    assert submitted.correlation_id == "s1"
    assert submitted.causation_id == confirmed.event_id


def test_invalid_setup_transition_is_rejected_directly():
    from datetime import datetime

    registry = SetupRegistry()
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registry.apply(SetupEvent("m", "s", SetupStatus.FILLED), now)
    with pytest.raises(SetupTransitionError, match="invalid setup transition"):
        registry.apply(SetupEvent("m", "s", SetupStatus.ARMED), now)
