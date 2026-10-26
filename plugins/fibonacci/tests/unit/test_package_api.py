from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from types import ModuleType
from typing import Mapping
import importlib
import sys


@dataclass(frozen=True)
class _Feature:
    key: str
    source_module: str
    created_at: datetime
    value: object
    symbol: str = "*"
    timeframe: str | None = None
    valid_until: datetime | None = None
    schema_version: str = "1"
    feature_id: str | None = None
    invalidate_on_gap: bool = True
    metadata: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _FeatureDelete:
    key: str
    source_module: str
    feature_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    reason: str = "module_invalidation"


@dataclass(frozen=True)
class _Decision:
    features: tuple[object, ...] = ()
    feature_deletes: tuple[object, ...] = ()
    skips: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class _Manifest:
    name: str
    version: str = "0.1.0"
    api_version: str = "1.0"
    required_timeframes: tuple[str, ...] = ()
    required_features: tuple[str, ...] = ()
    optional_features: tuple[str, ...] = ()
    produced_features: tuple[str, ...] = ()
    warmup_bars: Mapping[str, int] = field(default_factory=dict)
    parameters_schema: Mapping[str, Mapping[str, object]] = field(default_factory=dict)
    skip_until_ready: bool = True
    description: str = ""
    state_schema_version: str = "1"


class _StrategyModule:
    pass


def test_core_exports_are_lazy(monkeypatch) -> None:
    package = importlib.import_module("modular_strategy_fibonacci")
    for name in (
        "FibonacciModule",
        "FibonacciCoreStateError",
        "core_adapter",
        "core_adapter_v4",
    ):
        package.__dict__.pop(name, None)
    sys.modules.pop("modular_strategy_fibonacci.core_adapter", None)
    sys.modules.pop("modular_strategy_fibonacci.core_adapter_v4", None)
    assert package.__version__ == "0.4.0"
    assert "modular_strategy_fibonacci.core_adapter" not in sys.modules
    assert "modular_strategy_fibonacci.core_adapter_v4" not in sys.modules

    fake = ModuleType("modular_core")
    fake.Feature = _Feature
    fake.FeatureDelete = _FeatureDelete
    fake.MarketView = object
    fake.ModuleDecision = _Decision
    fake.ModuleManifest = _Manifest
    fake.StrategyModule = _StrategyModule
    monkeypatch.setitem(sys.modules, "modular_core", fake)

    assert package.FibonacciModule.__name__ == "FibonacciModule"
    assert package.FibonacciModule.__module__.endswith("core_adapter_v4")
    assert package.ACTIVE_PROJECTION_KEY == "fibonacci.active_projection"


def test_unknown_lazy_export_raises_attribute_error() -> None:
    package = importlib.import_module("modular_strategy_fibonacci")
    try:
        getattr(package, "does_not_exist")
    except AttributeError as exc:
        assert exc.args == ("does_not_exist",)
    else:
        raise AssertionError("unknown package attribute must fail")
