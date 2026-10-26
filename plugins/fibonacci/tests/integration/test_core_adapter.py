from __future__ import annotations

import copy
import importlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import ModuleType, SimpleNamespace
from typing import Mapping

import pytest

from modular_strategy_fibonacci.config import FibonacciConfig
from modular_strategy_fibonacci.contracts import WorkflowPhase
from modular_strategy_fibonacci.workflow.state_machine import SequentialFibonacciEngine


@dataclass(frozen=True)
class FakeFeature:
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
class FakeFeatureDelete:
    key: str
    source_module: str
    feature_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    reason: str = "module_invalidation"


@dataclass(frozen=True)
class FakeModuleDecision:
    features: tuple[FakeFeature, ...] = ()
    feature_deletes: tuple[FakeFeatureDelete, ...] = ()
    skips: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FakeModuleManifest:
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


class FakeStrategyModule:
    pass


@dataclass(frozen=True)
class FakeCandle:
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    symbol: str
    timeframe: str

    @property
    def close_time(self) -> datetime:
        minutes = {"15m": 15, "4h": 240, "1d": 1440}[self.timeframe]
        return self.timestamp + timedelta(minutes=minutes)


@dataclass(frozen=True)
class FakeMarketView:
    base_bars: tuple[FakeCandle, ...]
    timeframe_bars: Mapping[str, tuple[FakeCandle, ...]]
    index: int
    data_gap: bool = False

    @property
    def current(self) -> FakeCandle:
        return self.base_bars[-1]


def load_adapter(monkeypatch):
    fake = ModuleType("modular_core")
    fake.Feature = FakeFeature
    fake.FeatureDelete = FakeFeatureDelete
    fake.MarketView = FakeMarketView
    fake.ModuleDecision = FakeModuleDecision
    fake.ModuleManifest = FakeModuleManifest
    fake.StrategyModule = FakeStrategyModule
    monkeypatch.setitem(sys.modules, "modular_core", fake)
    sys.modules.pop("modular_strategy_fibonacci.core_adapter", None)
    return importlib.import_module("modular_strategy_fibonacci.core_adapter")


def core_candle(index: int, o: float, h: float, low: float, c: float) -> FakeCandle:
    return FakeCandle(
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        symbol="BTCUSDT",
        timeframe="15m",
    )


def seeded_engine() -> SequentialFibonacciEngine:
    engine = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(),
    )
    engine.seed_wave(
        direction="bullish",
        start_time=datetime(2024, 12, 31, 22, tzinfo=timezone.utc),
        start_price=Decimal("100"),
        end_time=datetime(2024, 12, 31, 23, tzinfo=timezone.utc),
        end_price=Decimal("150"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )
    return engine


def test_core_adapter_publishes_revised_projection_and_completed_lifecycle(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._engines[("BTCUSDT", "15m")] = seeded_engine()

    correction = core_candle(0, 145, 149, 125, 130)
    first = module.on_bar(FakeMarketView((correction,), {}, 0))
    assert len(first.features) == 1
    assert first.features[0].key == adapter.ACTIVE_PROJECTION_KEY
    assert first.features[0].value["endpoint_confirmed_at"] is not None
    first_id = first.features[0].feature_id

    target = core_candle(1, 130, 160, 128, 158)
    second = module.on_bar(FakeMarketView((correction, target), {}, 1))
    assert any(item.key == adapter.COMPLETED_WAVE_KEY for item in second.features)
    active = next(item for item in second.features if item.key == adapter.ACTIVE_PROJECTION_KEY)
    assert active.feature_id != first_id
    assert any(item.feature_id == first_id for item in second.feature_deletes)
    assert not hasattr(second, "orders") or not second.orders


def test_core_adapter_state_roundtrip_republishes_after_restore(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._engines[("BTCUSDT", "15m")] = seeded_engine()
    bar = core_candle(0, 145, 149, 125, 130)
    module.on_bar(FakeMarketView((bar,), {}, 0))
    state = module.snapshot_state()

    restored = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    restored.restore_state(state)
    decision = restored.on_bar(FakeMarketView((bar,), {}, 0))
    assert any(item.key == adapter.ACTIVE_PROJECTION_KEY for item in decision.features)


def test_core_adapter_gap_deletes_active_projection(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._engines[("BTCUSDT", "15m")] = seeded_engine()
    bar = core_candle(0, 145, 149, 125, 130)
    first = module.on_bar(FakeMarketView((bar,), {}, 0))
    active_id = first.features[0].feature_id

    gap = module.on_bar(FakeMarketView((bar,), {}, 1, data_gap=True))
    assert any(item.feature_id == active_id for item in gap.feature_deletes)
    assert "fibonacci.data_gap_reset" in gap.skips


def test_core_adapter_gap_barrier_prevents_old_history_resurrection(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._engines[("BTCUSDT", "15m")] = seeded_engine()
    old = core_candle(0, 145, 149, 125, 130)
    module.on_bar(FakeMarketView((old,), {}, 0))

    module.on_bar(FakeMarketView((old,), {}, 1, data_gap=True))
    same_history = module.on_bar(FakeMarketView((old,), {}, 2))
    assert same_history.features == ()
    assert same_history.diagnostics["processed_bars"] == 0

    new = core_candle(1, 130, 135, 128, 133)
    resumed = module.on_bar(FakeMarketView((old, new), {}, 3))
    assert resumed.diagnostics["processed_bars"] == 1
    assert ("BTCUSDT", "15m") not in module._cursor_barriers


def flexible_core_candle(
    index: int,
    o: float,
    h: float,
    low: float,
    c: float,
    *,
    timeframe: str = "15m",
    symbol: str = "BTCUSDT",
) -> FakeCandle:
    minutes = {"15m": 15, "4h": 240, "1d": 1440}[timeframe]
    return FakeCandle(
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=minutes * index),
        open=o,
        high=h,
        low=low,
        close=c,
        volume=1.0,
        symbol=symbol,
        timeframe=timeframe,
    )


def test_core_adapter_rejects_empty_timeframes(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    with pytest.raises(ValueError, match="timeframes"):
        adapter.FibonacciModule(timeframes=())


def test_core_adapter_empty_and_missing_series_are_explicit_skips(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("4h",), dataset_hash="dataset-v1")
    empty = module.on_bar(FakeMarketView((), {}, 0))
    assert empty.skips == ("fibonacci.empty_base_bars",)
    current = flexible_core_candle(0, 100, 102, 99, 101)
    missing = module.on_bar(FakeMarketView((current,), {}, 1))
    assert "fibonacci.no_data:4h" in missing.skips


def test_core_adapter_rejects_symbol_mismatch_and_future_htf(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    current = flexible_core_candle(0, 100, 102, 99, 101)
    module = adapter.FibonacciModule(timeframes=("4h",), dataset_hash="dataset-v1")
    wrong_symbol = flexible_core_candle(0, 100, 102, 99, 101, timeframe="4h", symbol="ETHUSDT")
    with pytest.raises(ValueError, match="symbol"):
        module.on_bar(FakeMarketView((current,), {"4h": (wrong_symbol,)}, 0))
    future = flexible_core_candle(0, 100, 102, 99, 101, timeframe="4h")
    with pytest.raises(ValueError, match="future"):
        module.on_bar(FakeMarketView((current,), {"4h": (future,)}, 0))


def test_core_adapter_rejects_temporal_regression(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    later = flexible_core_candle(2, 100, 102, 99, 101)
    module.on_bar(FakeMarketView((later,), {}, 2))
    earlier = flexible_core_candle(1, 100, 102, 99, 101)
    with pytest.raises(ValueError, match="regressed"):
        module.on_bar(FakeMarketView((earlier,), {}, 3))


def test_core_adapter_deletes_stale_active_id_when_engine_has_no_wave(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._active_feature_ids[("BTCUSDT", "15m")] = "old-active"
    bar = flexible_core_candle(0, 100, 102, 99, 101)
    decision = module.on_bar(FakeMarketView((bar,), {}, 0))
    assert any(item.feature_id == "old-active" for item in decision.feature_deletes)


def test_core_adapter_short_setup_publish_and_delete(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("4h",), dataset_hash="dataset-v1")
    setup = SimpleNamespace(
        setup_id="short-setup-1",
        wave_id="wave-1",
        armed_at=datetime(2025, 1, 1, 4, tzinfo=timezone.utc),
        correction_level=Decimal("0.618"),
        correction_ratio=Decimal("0.63"),
        correction_price=Decimal("120"),
        impulse_start_price=Decimal("150"),
        impulse_end_price=Decimal("100"),
        historical_success_rate=0.75,
        historical_sample_size=20,
        current_success_streak=4,
        evidence=("repeated bearish reaction",),
    )
    monkeypatch.setattr(adapter, "build_bearish_reaction_profile", lambda snapshot: object())
    monkeypatch.setattr(adapter, "active_bearish_continuation_setup", lambda snapshot, profile: setup)
    bar = flexible_core_candle(0, 100, 102, 99, 101, timeframe="4h")
    published = module.on_bar(FakeMarketView((bar,), {}, 0))
    feature = next(item for item in published.features if item.key == adapter.SHORT_SETUP_KEY)
    assert feature.feature_id == "short-setup-1"
    assert feature.metadata["execution"] == "none"

    monkeypatch.setattr(adapter, "active_bearish_continuation_setup", lambda snapshot, profile: None)
    deleted = module.on_bar(FakeMarketView((bar,), {}, 1))
    assert any(item.feature_id == "short-setup-1" for item in deleted.feature_deletes)


def _module_state(adapter):
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._engines[("BTCUSDT", "15m")] = seeded_engine()
    module._active_feature_ids[("BTCUSDT", "15m")] = "active-1"
    module._short_feature_ids[("BTCUSDT", "15m")] = "short-1"
    module._published_completed_ids.add("completed-1")
    module._cursor_barriers[("BTCUSDT", "15m")] = datetime(2025, 1, 1, tzinfo=timezone.utc)
    return module, copy.deepcopy(module.snapshot_state())


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda state: state.pop("engines"), "keys"),
        (lambda state: state.__setitem__("state_schema_version", "999"), "schema"),
        (lambda state: state.__setitem__("module_version", "999"), "version"),
        (lambda state: state.__setitem__("config_fingerprint", "wrong"), "configuration"),
        (lambda state: state.__setitem__("engines", "bad"), "lists"),
        (lambda state: state.__setitem__("published_completed_ids", ["", ""]), "completed"),
    ],
)
def test_core_restore_rejects_top_level_corruption(monkeypatch, mutation, message) -> None:
    adapter = load_adapter(monkeypatch)
    module, state = _module_state(adapter)
    mutation(state)
    with pytest.raises(adapter.FibonacciCoreStateError, match=message):
        module.restore_state(state)


def test_core_restore_rejects_bad_engine_rows(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module, state = _module_state(adapter)
    cases = []
    bad = copy.deepcopy(state)
    bad["engines"] = [{}]
    cases.append((bad, "engine state row"))
    bad = copy.deepcopy(state)
    bad["engines"][0]["timeframe"] = "1h"
    cases.append((bad, "unconfigured"))
    bad = copy.deepcopy(state)
    bad["engines"][0]["state"] = "bad"
    cases.append((bad, "mapping"))
    bad = copy.deepcopy(state)
    bad["engines"][0]["symbol"] = "ETHUSDT"
    cases.append((bad, "identity"))
    for payload, message in cases:
        with pytest.raises(adapter.FibonacciCoreStateError, match=message):
            module.restore_state(payload)


def test_core_restore_rejects_bad_feature_ids(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module, state = _module_state(adapter)
    bad = copy.deepcopy(state)
    bad["active_feature_ids"] = [{}]
    with pytest.raises(adapter.FibonacciCoreStateError, match="active"):
        module.restore_state(bad)
    bad = copy.deepcopy(state)
    bad["short_feature_ids"] = [{"symbol": "BTCUSDT", "timeframe": "15m", "feature_id": ""}]
    with pytest.raises(adapter.FibonacciCoreStateError, match="short"):
        module.restore_state(bad)


def test_core_restore_rejects_bad_barriers(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module, state = _module_state(adapter)
    cases = []
    bad = copy.deepcopy(state)
    bad["cursor_barriers"] = [{}]
    cases.append((bad, "barrier row"))
    bad = copy.deepcopy(state)
    bad["cursor_barriers"][0]["open_time"] = "nope"
    cases.append((bad, "timestamp"))
    bad = copy.deepcopy(state)
    bad["cursor_barriers"][0]["open_time"] = "2025-01-01T00:00:00"
    cases.append((bad, "timezone-aware"))
    bad = copy.deepcopy(state)
    bad["cursor_barriers"][0]["timeframe"] = "1h"
    cases.append((bad, "unconfigured"))
    for payload, message in cases:
        with pytest.raises(adapter.FibonacciCoreStateError, match=message):
            module.restore_state(payload)


def _freeze_core_payload(value):
    from collections.abc import Mapping as AbcMapping
    from types import MappingProxyType

    if isinstance(value, AbcMapping):
        return MappingProxyType({key: _freeze_core_payload(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_core_payload(item) for item in value)
    return value


def test_core_adapter_restore_accepts_deep_frozen_module_state(monkeypatch) -> None:
    adapter = load_adapter(monkeypatch)
    module = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    module._engines[("BTCUSDT", "15m")] = seeded_engine()
    frozen = _freeze_core_payload(module.snapshot_state())

    restored = adapter.FibonacciModule(timeframes=("15m",), dataset_hash="dataset-v1")
    restored.restore_state(frozen)
    assert restored.snapshot_state() == module.snapshot_state()
