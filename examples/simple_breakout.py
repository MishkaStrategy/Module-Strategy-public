from modular_core import (
    MarketView,
    ModuleDecision,
    OrderRequest,
    OrderType,
    Side,
    Signal,
    StrategyModule,
)


class SimpleBreakoutModule(StrategyModule):
    """Example plugin only; not part of the kernel's trading logic."""

    name = "simple_breakout_example"

    def __init__(self, lookback: int = 3) -> None:
        self.lookback = lookback

    def on_bar(self, view: MarketView) -> ModuleDecision:
        previous = view.history(self.lookback, include_current=False)
        if len(previous) < self.lookback:
            return ModuleDecision(skips=("warmup",))
        if view.position is not None or view.pending_order_count:
            return ModuleDecision(skips=("busy",))

        current = view.current
        previous_high = max(b.high for b in previous)
        if current.close <= previous_high:
            return ModuleDecision()

        stop = current.close * 0.99
        target = current.close * 1.02
        return ModuleDecision(
            signals=(Signal(self.name, Side.BUY, "close_above_lookback_high"),),
            orders=(
                OrderRequest(
                    side=Side.BUY,
                    order_type=OrderType.MARKET,
                    stop_loss=stop,
                    take_profit=target,
                    risk_fraction=0.005,
                    tag="example-breakout",
                ),
            ),
        )
