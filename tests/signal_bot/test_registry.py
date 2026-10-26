from __future__ import annotations

import pytest

from signal_bot.registry import StrategyRegistry, StrategyRegistryError


class Strategy:
    strategy_id = "safe_strategy"
    version = "1.0.0"
    required_timeframes = ("5m",)

    def __init__(self, *, length: int = 20) -> None:
        self.length = length

    def warmup_bars(self, timeframe: str) -> int:
        return self.length

    def evaluate(self, context):
        raise NotImplementedError


def test_registry_is_explicit_deterministic_and_versioned() -> None:
    registry = StrategyRegistry()
    registry.register("safe_strategy", Strategy)
    strategy = registry.create("safe_strategy", "1.0.0", {"length": 30})
    assert strategy.strategy_id == "safe_strategy" and strategy.length == 30
    with pytest.raises(StrategyRegistryError, match="duplicate"):
        registry.register("safe_strategy", Strategy)
    with pytest.raises(StrategyRegistryError, match="unknown"):
        registry.create("os.system", "1.0.0", {})
    with pytest.raises(StrategyRegistryError, match="version mismatch"):
        registry.create("safe_strategy", "9.9.9", {})
    with pytest.raises(StrategyRegistryError, match="invalid strategy ID"):
        registry.register("os.system", Strategy)

    class WrongStrategy(Strategy):
        strategy_id = "other"

    wrong = StrategyRegistry()
    wrong.register("safe_strategy", WrongStrategy)
    with pytest.raises(StrategyRegistryError, match="does not match"):
        wrong.create("safe_strategy", "1.0.0", {})
