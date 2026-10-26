from __future__ import annotations

from modular_core.manifest import ModuleManifest
from modular_core.module_api import MarketView, ModuleDecision, StrategyModule


class AcceptanceNoopModule(StrategyModule):
    """Deterministic no-op module for registered-data acceptance smoke."""

    name = "persistent_acceptance_noop"
    manifest = ModuleManifest(
        name=name,
        description="No-op strategy module for registered-data acceptance smoke.",
    )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        del view
        return ModuleDecision()
