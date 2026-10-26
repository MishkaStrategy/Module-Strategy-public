from modular_core import (
    BacktestEngine,
    ClosePositionRequest,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    PositionUpdateRequest,
    SetupEvent,
    SetupStatus,
    Side,
    StrategyModule,
)


class ManagedPosition(StrategyModule):
    name = "managed"

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
                        take_profit=120,
                        setup_id="s1",
                    ),
                ),
            )
        if view.index == 1 and view.position is not None:
            return ModuleDecision(
                position_updates=(
                    PositionUpdateRequest(stop_loss=99, take_profit=120, reason="protect"),
                ),
                closes=(ClosePositionRequest(reason="time_exit"),),
            )
        return ModuleDecision()


def test_position_update_and_close_execute_safely_on_next_bar(bars_factory):
    bars = bars_factory(
        [
            (100, 101, 99, 100),
            (100, 101, 99.5, 100),
            (102, 103, 101, 102),
        ]
    )
    result = BacktestEngine(ManagedPosition()).run(bars)
    trade = result.journal.trades[0]
    assert trade.exit_reason == "time_exit"
    assert trade.exit_price == 102
    statuses = [row["setup"].status.value for row in result.journal.setups]
    assert statuses[:3] == ["confirmed", "ordered", "filled"]
    events = [row["values"]["event"] for row in result.journal.diagnostics]
    assert "position_updated" in events
    assert "position_close_scheduled" in events


class MoveStop(StrategyModule):
    name = "move_stop"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(
                    OrderRequest(
                        Side.BUY,
                        OrderType.MARKET,
                        quantity=1,
                        stop_loss=95,
                        take_profit=120,
                    ),
                )
            )
        if view.index == 1 and view.position is not None:
            return ModuleDecision(
                position_updates=(PositionUpdateRequest(stop_loss=99, take_profit=120),)
            )
        return ModuleDecision()


def test_updated_stop_is_effective_from_following_bar(bars_factory):
    bars = bars_factory(
        [
            (100, 101, 99, 100),
            (100, 101, 99.5, 100),
            (100, 101, 98, 99),
        ]
    )
    result = BacktestEngine(MoveStop()).run(bars)
    assert result.journal.trades[0].exit_reason == "stop"
    assert result.journal.trades[0].exit_price == 99


class InvalidUpdate(StrategyModule):
    name = "invalid_update"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if view.index == 0:
            return ModuleDecision(
                orders=(OrderRequest(Side.BUY, OrderType.MARKET, quantity=1, stop_loss=95),)
            )
        if view.index == 1 and view.position is not None:
            return ModuleDecision(
                position_updates=(PositionUpdateRequest(stop_loss=101, take_profit=None),)
            )
        return ModuleDecision()


def test_invalid_position_update_is_rejected(bars_factory):
    result = BacktestEngine(InvalidUpdate()).run(bars_factory([100, 100, 100]))
    reasons = [row["reason"] for row in result.journal.skips]
    assert "invalid_position_update" in reasons
