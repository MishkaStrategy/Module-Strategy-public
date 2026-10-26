from modular_core import (
    ClosePositionRequest,
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    PositionUpdateRequest,
    SetupEvent,
    SetupStatus,
    Side,
    Signal,
    StrategyModule,
)


class BreakoutSetup(StrategyModule):
    name = "breakout_setup"

    def __init__(self) -> None:
        self.emitted = False

    def on_bar(self, view: MarketView) -> ModuleDecision:
        previous = view.history(3, include_current=False)
        if self.emitted or len(previous) < 3:
            return ModuleDecision()
        if view.current.close <= max(bar.high for bar in previous):
            return ModuleDecision()

        self.emitted = True
        setup_id = f"breakout-{view.current.timestamp.isoformat()}"
        return ModuleDecision(
            setups=(SetupEvent(self.name, setup_id, SetupStatus.CONFIRMED),),
            signals=(Signal(self.name, Side.BUY, "close_above_3bar_high"),),
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    stop_loss=view.current.close * 0.99,
                    take_profit=view.current.close * 1.02,
                    risk_fraction=0.005,
                    setup_id=setup_id,
                    client_order_id=setup_id,
                    tag="breakout",
                ),
            ),
        )


class PositionManager(StrategyModule):
    name = "position_manager"

    def on_bar(self, view: MarketView) -> ModuleDecision:
        position = view.position
        if position is None:
            return ModuleDecision()

        # Example only: move stop to entry after +1% unrealized move.
        if view.current.close >= position.entry_price * 1.01:
            return ModuleDecision(
                position_updates=(
                    PositionUpdateRequest(
                        stop_loss=position.entry_price,
                        take_profit=position.take_profit,
                        reason="breakeven",
                    ),
                )
            )

        # Example time exit after the module's own condition.
        if view.index % 24 == 0 and not view.exit_pending:
            return ModuleDecision(closes=(ClosePositionRequest("manager_time_exit"),))
        return ModuleDecision()


# Usage:
# engine = BacktestEngine(
#     [BreakoutSetup(), PositionManager()],
#     timeframes=(TimeframeSpec(3, "15m", gap_safe=True),),
# )
# result = engine.run(candles)
# result.export_directory("audit/run_001")
