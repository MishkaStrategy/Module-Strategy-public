from __future__ import annotations

from importlib.metadata import entry_points
from typing import Any, Callable, Mapping

from .contracts import SignalStrategy


class StrategyRegistryError(ValueError):
    pass


class StrategyRegistry:
    """Explicit allowlisted factories; YAML never controls Python imports."""

    def __init__(self) -> None:
        self._factories: dict[str, Callable[..., SignalStrategy]] = {}

    def register(self, strategy_id: str, factory: Callable[..., SignalStrategy]) -> None:
        if not strategy_id or not strategy_id.replace("_", "").replace("-", "").isalnum():
            raise StrategyRegistryError("invalid strategy ID")
        if strategy_id in self._factories:
            raise StrategyRegistryError(f"duplicate strategy ID: {strategy_id}")
        self._factories[strategy_id] = factory

    def discover(self, group: str = "modular_strategy.signal_strategies") -> tuple[str, ...]:
        names = []
        for item in sorted(entry_points().select(group=group), key=lambda value: value.name):
            self.register(item.name, item.load())
            names.append(item.name)
        return tuple(names)

    def create(
        self, strategy_id: str, version: str, parameters: Mapping[str, Any]
    ) -> SignalStrategy:
        factory = self._factories.get(strategy_id)
        if factory is None:
            raise StrategyRegistryError(f"unknown strategy ID: {strategy_id}")
        strategy = factory(**dict(parameters))
        if strategy.strategy_id != strategy_id:
            raise StrategyRegistryError("factory strategy_id does not match registration")
        if strategy.version != version:
            raise StrategyRegistryError(
                f"strategy version mismatch: configured {version}, loaded {strategy.version}"
            )
        return strategy
