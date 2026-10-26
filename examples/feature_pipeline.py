from modular_core import Feature, ModuleDecision, ModuleManifest, StrategyModule


class RegimeModule(StrategyModule):
    name = "regime"
    manifest = ModuleManifest(
        name=name,
        produced_features=("regime.current",),
        warmup_bars={"5m": 2},
    )

    def on_bar(self, view):
        direction = "up" if view.current.close >= view.history(2)[0].close else "down"
        return ModuleDecision(
            features=(
                Feature(
                    key="regime.current",
                    source_module=self.name,
                    created_at=view.current.close_time,
                    value=direction,
                    symbol=view.current.symbol,
                    timeframe=view.current.timeframe,
                ),
            )
        )


class ConsumerModule(StrategyModule):
    name = "consumer"
    manifest = ModuleManifest(name=name, required_features=("regime.current",))

    def on_bar(self, view):
        return ModuleDecision(diagnostics={"regime": view.features.get("regime.current")})
