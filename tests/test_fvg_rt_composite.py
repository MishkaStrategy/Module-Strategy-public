from __future__ import annotations

from datetime import UTC, datetime

import pytest
from modular_core import Feature, FeatureDelete, ModuleDecision, ModuleRegistry
from strategy_modules.fvg_rt import (
    COMPOSITE_CONTRACT,
    COMPOSITE_MODULE_NAME,
    COMPOSITE_MODULE_VERSION,
    FROZEN_1H_FEATURE_KEY,
    FROZEN_4H_FEATURE_KEY,
    FEATURE_KEY,
    FvgRtCompositeModule,
    FvgRtFinal15mModule,
)

NOW = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)


def _final15_feature() -> Feature:
    module = FvgRtFinal15mModule()
    snapshot = module._snapshot(
        "long",
        "BTCUSDT",
        NOW,
        entry=100.0,
        stop=95.0,
    )
    assert snapshot is not None
    return module._feature(snapshot, NOW, "BTCUSDT")


@pytest.mark.parametrize(
    "component_ids",
    [
        ("fvg_rt_final_15m", "frozen_1h_short", "frozen_4h_short"),
        ["fvg_rt_final_15m", "frozen_1h_short", "frozen_4h_short"],
    ],
)
def test_composite_accepts_canonical_fixed_constructor_parameters(
    component_ids: tuple[str, str, str] | list[str],
) -> None:
    module = FvgRtCompositeModule(
        contract=COMPOSITE_CONTRACT,
        component_ids=component_ids,
    )

    assert module.parameters == {
        "contract": COMPOSITE_CONTRACT,
        "component_ids": (
            "fvg_rt_final_15m",
            "frozen_1h_short",
            "frozen_4h_short",
        ),
    }


@pytest.mark.parametrize(
    ("contract", "component_ids", "message"),
    [
        (
            "unexpected-contract",
            ("fvg_rt_final_15m", "frozen_1h_short", "frozen_4h_short"),
            "contract drift",
        ),
        (
            COMPOSITE_CONTRACT,
            ("frozen_1h_short", "fvg_rt_final_15m", "frozen_4h_short"),
            "component identity drift",
        ),
        (
            COMPOSITE_CONTRACT,
            ["fvg_rt_final_15m", "frozen_1h_short"],
            "component identity drift",
        ),
    ],
)
def test_composite_rejects_noncanonical_fixed_constructor_parameters(
    contract: str,
    component_ids: tuple[str, ...] | list[str],
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        FvgRtCompositeModule(contract=contract, component_ids=component_ids)


def test_core_module_registry_instantiates_composite_with_fixed_parameters() -> None:
    modules = ModuleRegistry().load_config(
        {
            "modules": [
                {
                    "class": "strategy_modules.fvg_rt:FvgRtCompositeModule",
                    "parameters": {
                        "contract": COMPOSITE_CONTRACT,
                        "component_ids": [
                            "fvg_rt_final_15m",
                            "frozen_1h_short",
                            "frozen_4h_short",
                        ],
                    },
                }
            ]
        }
    )

    assert len(modules) == 1
    module = modules[0]
    assert isinstance(module, FvgRtCompositeModule)
    assert module.parameters["contract"] == COMPOSITE_CONTRACT
    assert module.parameters["component_ids"] == (
        "fvg_rt_final_15m",
        "frozen_1h_short",
        "frozen_4h_short",
    )


def test_composite_manifest_exposes_exact_three_component_surface() -> None:
    module = FvgRtCompositeModule()

    assert module.manifest is not None
    assert module.manifest.name == COMPOSITE_MODULE_NAME
    assert module.manifest.version == COMPOSITE_MODULE_VERSION
    assert module.manifest.produced_features == (
        FEATURE_KEY,
        FROZEN_1H_FEATURE_KEY,
        FROZEN_4H_FEATURE_KEY,
    )
    assert module.manifest.required_timeframes == ("15m", "1h", "4h", "1d")
    assert module.parameters["component_ids"] == (
        "fvg_rt_final_15m",
        "frozen_1h_short",
        "frozen_4h_short",
    )


def test_composite_reemits_child_snapshot_with_core_emitter_provenance() -> None:
    module = FvgRtCompositeModule()
    child = module._components[0]
    original = _final15_feature()

    emitted = module._reemit_feature(child, original)

    assert emitted.source_module == COMPOSITE_MODULE_NAME
    assert emitted.key == original.key
    assert emitted.feature_id == original.feature_id
    assert emitted.created_at == original.created_at
    assert emitted.valid_until == original.valid_until
    assert emitted.value == original.value
    assert emitted.value["module_id"] == "fvg_rt_final_15m"
    assert emitted.value["module_version"] == "2.0.0"
    assert emitted.value["semantic_input_digest"] == original.value["semantic_input_digest"]


def test_composite_fails_closed_if_child_snapshot_identity_drifts() -> None:
    module = FvgRtCompositeModule()
    child = module._components[0]
    original = _final15_feature()
    drifted = Feature(
        key=original.key,
        source_module=original.source_module,
        created_at=original.created_at,
        value={**dict(original.value), "module_id": COMPOSITE_MODULE_NAME},
        symbol=original.symbol,
        timeframe=original.timeframe,
        valid_until=original.valid_until,
        schema_version=original.schema_version,
        feature_id=original.feature_id,
        invalidate_on_gap=original.invalidate_on_gap,
        metadata=original.metadata,
    )

    with pytest.raises(RuntimeError, match="child snapshot module identity drift"):
        module._reemit_feature(child, drifted)


def test_composite_rejects_child_executable_side_effects() -> None:
    module = FvgRtCompositeModule()
    child = module._components[0]
    decision = ModuleDecision(signals=())

    module._validate_component_decision(child, decision)

    with pytest.raises(RuntimeError, match="unsupported side effects"):
        module._validate_component_decision(
            child,
            ModuleDecision(
                feature_deletes=(
                    FeatureDelete(
                        key=FEATURE_KEY,
                        source_module=child.name,
                    ),
                )
            ),
        )


def test_composite_state_round_trip_preserves_component_identity() -> None:
    module = FvgRtCompositeModule()
    state = module.snapshot_state()

    restored = FvgRtCompositeModule()
    restored.restore_state(state)

    assert restored.snapshot_state() == state


def test_composite_state_rejects_missing_component() -> None:
    module = FvgRtCompositeModule()
    state = dict(module.snapshot_state())
    components = dict(state["components"])
    components.pop("frozen_4h_short")
    state["components"] = components

    with pytest.raises(ValueError, match="component state identity drift"):
        module.restore_state(state)
