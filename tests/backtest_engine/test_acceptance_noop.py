from __future__ import annotations

from typing import cast

from modular_backtest.acceptance import AcceptanceNoopModule
from modular_core.module_api import MarketView, ModuleDecision


def test_acceptance_noop_module_is_deterministic() -> None:
    module = AcceptanceNoopModule()

    assert module.name == "persistent_acceptance_noop"
    assert module.manifest is not None
    assert module.manifest.name == module.name
    assert module.on_bar(cast(MarketView, object())) == ModuleDecision()
