from __future__ import annotations

from decimal import Decimal

import pytest
from modular_core import ModuleRegistry

from modular_strategy_protection_target import (
    InvalidationMode,
    ProtectionTargetConfig,
    ProtectionTargetConfigurationError,
    ProtectionTargetModule,
    StructuralTargetSource,
    TargetMode,
)


def test_default_config_and_manifest() -> None:
    module = ProtectionTargetModule()
    assert module.version == "1.1.1"
    assert module.state_schema_version == "2"
    assert module.config.fixed_stop_percent == Decimal("0.005")
    assert module.manifest.required_features == ("entry_model.entry_plan",)
    assert module.manifest.produced_features == (
        "protection_target.plan",
        "protection_target.rejected",
    )
    assert module.manifest.required_timeframes == ()
    assert module.manifest.warmup_bars == {}


def test_dynamic_dependencies_are_deduplicated() -> None:
    module = ProtectionTargetModule(
        invalidation_mode="feature",
        invalidation_feature_key="structure.level",
        target_mode="structural",
        structural_target_source="feature",
        target_feature_key="structure.level",
    )
    assert module.manifest.required_features == (
        "entry_model.entry_plan",
        "structure.level",
    )


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"entry_feature_key": "plain"}, "namespaced"),
        ({"invalidation_field": "bad path"}, "dotted"),
        ({"invalidation_mode": "feature"}, "invalidation_feature_key"),
        (
            {"target_mode": "structural", "structural_target_source": "feature"},
            "target_feature_key",
        ),
        ({"fixed_stop_percent": 0}, "fixed_stop_percent"),
        ({"fixed_stop_percent": 1}, "fixed_stop_percent"),
        ({"stop_buffer_bps": -1}, "stop_buffer_bps"),
        ({"stop_buffer_bps": 10000}, "stop_buffer_bps"),
        ({"reward_r": 0}, "reward_r"),
        ({"reward_r": 50}, "within"),
        ({"min_reward_r": 2, "max_reward_r": 1}, "reward-R"),
        ({"min_stop_distance_bps": -1}, "min_stop_distance_bps"),
        ({"max_stop_distance_bps": 0}, "stop-distance"),
        ({"max_processed_plans": 0}, "max_processed_plans"),
        ({"max_processed_plans": True}, "integer"),
        ({"max_diagnostic_results": -1}, "max_diagnostic_results"),
        ({"max_diagnostic_results": 1001}, "max_diagnostic_results"),
        ({"max_diagnostic_results": True}, "integer"),
        ({"require_same_bar_entry": 1}, "boolean"),
        ({"require_structural_setup_match": 1}, "boolean"),
        ({"emit_rejection_feature": 1}, "boolean"),
    ],
)
def test_invalid_constructor_values(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ProtectionTargetConfigurationError, match=message):
        ProtectionTargetModule(**kwargs)


def test_enum_config_and_stable_fingerprints() -> None:
    first = ProtectionTargetConfig(
        invalidation_mode=InvalidationMode.FIXED_PERCENT,
        target_mode=TargetMode.STRUCTURAL,
        structural_target_source=StructuralTargetSource.ENTRY_PLAN,
        reward_r=Decimal("3.00"),
    )
    second = ProtectionTargetConfig(
        invalidation_mode=InvalidationMode.FIXED_PERCENT,
        target_mode=TargetMode.STRUCTURAL,
        structural_target_source=StructuralTargetSource.ENTRY_PLAN,
        reward_r=Decimal("3"),
    )
    assert first.fingerprint() == second.fingerprint()
    assert first.legacy_fingerprint_v1() == second.legacy_fingerprint_v1()
    assert first.fingerprint() != first.legacy_fingerprint_v1()


def test_manifest_validates_with_real_registry() -> None:
    module = ProtectionTargetModule()

    class EntryProducer:
        name = "entry_model"
        parameters: dict[str, object] = {}
        manifest = type(module.manifest)(
            name="entry_model",
            version="0.1.0",
            api_version="1.0",
            produced_features=("entry_model.entry_plan",),
        )

    planned = ModuleRegistry().validate_and_plan((module, EntryProducer()), base_timeframe="5m")
    assert [item.name for item in planned.modules] == [
        "entry_model",
        "protection_target",
    ]


def test_parameters_are_complete_and_registry_compatible() -> None:
    module = ProtectionTargetModule(max_diagnostic_results=7)
    assert module.parameters["max_diagnostic_results"] == 7
    assert set(module.parameters) <= set(module.manifest.parameters_schema)


def test_configuration_decimal_shape_is_bounded() -> None:
    with pytest.raises(ProtectionTargetConfigurationError, match="significant digits"):
        ProtectionTargetModule(max_reward_r="1" * 257, reward_r=1)
    with pytest.raises(ProtectionTargetConfigurationError, match="exponent"):
        ProtectionTargetModule(fixed_stop_percent="1E-4097")


def test_manifest_numbers_must_be_finite_and_non_underflowing() -> None:
    with pytest.raises(ProtectionTargetConfigurationError, match="finite Module API number"):
        ProtectionTargetModule(target_mode="structural", max_reward_r="1E+4000")
    with pytest.raises(ProtectionTargetConfigurationError, match="finite Module API number"):
        ProtectionTargetModule(fixed_stop_percent="1E-4000")


def test_manifest_schema_is_consistent_with_constructor_boundaries() -> None:
    module = ProtectionTargetModule(
        fixed_stop_percent="0.999999999",
        stop_buffer_bps="9999.9999999",
        reward_r="0.000000001",
        min_reward_r="0.000000001",
        max_reward_r="1",
    )

    class EntryProducer:
        name = "entry_model"
        parameters: dict[str, object] = {}
        manifest = type(module.manifest)(
            name="entry_model",
            version="0.1.0",
            api_version="1.0",
            produced_features=("entry_model.entry_plan",),
        )

    plan = ModuleRegistry().validate_and_plan((module, EntryProducer()), base_timeframe="5m")
    assert [item.name for item in plan.modules] == ["entry_model", "protection_target"]


def test_configuration_strings_are_bounded() -> None:
    with pytest.raises(ProtectionTargetConfigurationError, match="no longer than"):
        ProtectionTargetModule(entry_feature_key=f"namespace.{'x' * 600}")
    with pytest.raises(ProtectionTargetConfigurationError, match="exceeds"):
        ProtectionTargetModule(invalidation_field="x" * 600)
