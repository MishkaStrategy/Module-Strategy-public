from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from types import ModuleType

import pytest

from tests.corrections.test_short_setup_cycle4 import (
    _qualified_setup_snapshot,
    _snapshot,
)
from tests.integration.test_core_adapter import (
    FakeFeature,
    FakeFeatureDelete,
    FakeMarketView,
    FakeModuleDecision,
    FakeModuleManifest,
    FakeStrategyModule,
    flexible_core_candle,
)


class FakeEngine:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def snapshot(self, _cutoff):
        return self._snapshot


def load_v4(monkeypatch):
    fake = ModuleType("modular_core")
    fake.Feature = FakeFeature
    fake.FeatureDelete = FakeFeatureDelete
    fake.MarketView = FakeMarketView
    fake.ModuleDecision = FakeModuleDecision
    fake.ModuleManifest = FakeModuleManifest
    fake.StrategyModule = FakeStrategyModule
    monkeypatch.setitem(sys.modules, "modular_core", fake)
    sys.modules.pop("modular_strategy_fibonacci.core_adapter", None)
    sys.modules.pop("modular_strategy_fibonacci.core_adapter_v4", None)
    return importlib.import_module("modular_strategy_fibonacci.core_adapter_v4")


def module_with_permissive_regime(adapter):
    return adapter.FibonacciModule(
        timeframes=("4h",),
        dataset_hash="dataset-v4",
        short_reaction_minimum_samples=1,
        short_reaction_minimum_success_rate="0",
        short_reaction_minimum_streak=1,
        short_regime_window_size=4,
        short_regime_minimum_window_samples=1,
        short_regime_minimum_success_rate="0",
        short_regime_minimum_level_samples=1,
        short_regime_minimum_level_success_rate="0",
    )


def patch_base_decision(monkeypatch, adapter, decision=None) -> None:
    monkeypatch.setattr(
        adapter._CoreAuditedFibonacciModule,
        "on_bar",
        lambda self, view: decision or FakeModuleDecision(diagnostics={"short_setups_published": 0}),
    )


def view_4h(*, gap: bool = False, empty: bool = False) -> FakeMarketView:
    if empty:
        return FakeMarketView((), {}, 0, data_gap=gap)
    bar = flexible_core_candle(0, 100, 103, 99, 102, timeframe="4h")
    return FakeMarketView((bar,), {}, 0, data_gap=gap)


def test_v4_manifest_and_parameters_are_versioned(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    module = module_with_permissive_regime(adapter)
    assert module.manifest.version == "0.4.0"
    assert module.manifest.state_schema_version == "2"
    assert module.parameters["short_regime_window_size"] == 4
    assert module.parameters["short_regime_minimum_level_success_rate"] == "0"
    assert module.publish_short_setup is True


def test_v4_publishes_regime_aware_feature(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    patch_base_decision(monkeypatch, adapter)
    module = module_with_permissive_regime(adapter)
    module._engines[("BTCUSDT", "4h")] = FakeEngine(_qualified_setup_snapshot())
    decision = module.on_bar(view_4h())
    assert len(decision.features) == 1
    feature = decision.features[0]
    assert feature.key == adapter.SHORT_SETUP_KEY
    assert feature.schema_version == "2"
    assert feature.value["sequence_root_id"]
    assert feature.value["current_regime"] in {
        "improving",
        "stable",
        "insufficient_data",
    }
    assert feature.value["recent_level_sample_size"] >= 1
    assert feature.metadata["execution"] == "none"
    assert not hasattr(decision, "orders") or not decision.orders
    assert decision.diagnostics["short_setups_published"] == 1


def test_v4_deduplicates_same_setup(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    patch_base_decision(monkeypatch, adapter)
    module = module_with_permissive_regime(adapter)
    module._engines[("BTCUSDT", "4h")] = FakeEngine(_qualified_setup_snapshot())
    first = module.on_bar(view_4h())
    second = module.on_bar(view_4h())
    assert len(first.features) == 1
    assert second.features == ()
    assert second.feature_deletes == ()


def test_v4_revision_replaces_prior_setup(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    patch_base_decision(monkeypatch, adapter)
    module = module_with_permissive_regime(adapter)
    snapshot = _qualified_setup_snapshot()
    module._engines[("BTCUSDT", "4h")] = FakeEngine(snapshot)
    first = module.on_bar(view_4h())
    assert snapshot.active_wave is not None
    revised = replace(
        snapshot,
        active_wave=replace(
            snapshot.active_wave,
            endpoint_revision=snapshot.active_wave.endpoint_revision + 1,
        ),
    )
    module._engines[("BTCUSDT", "4h")] = FakeEngine(revised)
    second = module.on_bar(view_4h())
    assert len(second.features) == 1
    assert len(second.feature_deletes) == 1
    assert second.feature_deletes[0].feature_id == first.features[0].feature_id
    assert second.features[0].feature_id != first.features[0].feature_id


def test_v4_inactive_setup_deletes_previous_feature(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    patch_base_decision(monkeypatch, adapter)
    module = module_with_permissive_regime(adapter)
    key = ("BTCUSDT", "4h")
    module._engines[key] = FakeEngine(_snapshot([], active=False))
    module._short_feature_ids[key] = "old-setup"
    decision = module.on_bar(view_4h())
    assert decision.features == ()
    assert decision.feature_deletes[0].feature_id == "old-setup"
    assert key not in module._short_feature_ids


def test_v4_republishes_same_setup_after_restore_flag(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    patch_base_decision(monkeypatch, adapter)
    module = module_with_permissive_regime(adapter)
    module._engines[("BTCUSDT", "4h")] = FakeEngine(_qualified_setup_snapshot())
    first = module.on_bar(view_4h())
    module._republish_pending = True
    second = module.on_bar(view_4h())
    assert second.features[0].feature_id == first.features[0].feature_id


@pytest.mark.parametrize(
    "condition",
    ["disabled", "empty", "gap", "no_engine"],
)
def test_v4_short_processing_skip_paths(monkeypatch, condition: str) -> None:
    adapter = load_v4(monkeypatch)
    base = FakeModuleDecision(skips=("base",), diagnostics={"short_setups_published": 0})
    patch_base_decision(monkeypatch, adapter, base)
    module = module_with_permissive_regime(adapter)
    if condition == "disabled":
        module.publish_short_setup = False
        view = view_4h()
    elif condition == "empty":
        view = view_4h(empty=True)
    elif condition == "gap":
        view = view_4h(gap=True)
    else:
        view = view_4h()
    result = module.on_bar(view)
    if condition == "no_engine":
        assert result == base
    else:
        assert result is base


def test_v4_state_roundtrip_accepts_core_frozen_tuples(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    module = module_with_permissive_regime(adapter)
    state = dict(module.snapshot_state())
    for key in (
        "engines",
        "active_feature_ids",
        "short_feature_ids",
        "published_completed_ids",
        "cursor_barriers",
    ):
        state[key] = tuple(state[key])
    restored = module_with_permissive_regime(adapter)
    restored.restore_state(state)
    assert restored.snapshot_state()["module_version"] == "0.4.0"
    assert restored.snapshot_state()["state_schema_version"] == "2"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("module_version", "0.3.0", "module version"),
        ("state_schema_version", "1", "state schema"),
    ],
)
def test_v4_restore_rejects_wrong_version(
    monkeypatch,
    field: str,
    value: str,
    message: str,
) -> None:
    adapter = load_v4(monkeypatch)
    module = module_with_permissive_regime(adapter)
    state = dict(module.snapshot_state())
    state[field] = value
    with pytest.raises(adapter.FibonacciCoreStateError, match=message):
        module.restore_state(state)


def test_v4_threshold_change_changes_checkpoint_fingerprint(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    first = module_with_permissive_regime(adapter)
    second = adapter.FibonacciModule(
        timeframes=("4h",),
        dataset_hash="dataset-v4",
        short_reaction_minimum_samples=1,
        short_reaction_minimum_success_rate="0",
        short_reaction_minimum_streak=1,
        short_regime_window_size=5,
        short_regime_minimum_window_samples=1,
        short_regime_minimum_success_rate="0",
        short_regime_minimum_level_samples=1,
        short_regime_minimum_level_success_rate="0",
    )
    assert first.snapshot_state()["config_fingerprint"] != second.snapshot_state()["config_fingerprint"]


def test_package_lazy_entrypoint_resolves_v4(monkeypatch) -> None:
    adapter = load_v4(monkeypatch)
    package = importlib.import_module("modular_strategy_fibonacci")
    package.__dict__.pop("FibonacciModule", None)
    assert package.FibonacciModule is adapter.FibonacciModule
