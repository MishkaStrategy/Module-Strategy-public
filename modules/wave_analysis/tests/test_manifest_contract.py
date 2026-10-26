from __future__ import annotations

from collections.abc import Mapping

from modular_core import ModuleRegistry

from wave_analysis import MODULE_NAME, MODULE_VERSION, WaveAnalysisModule
from wave_analysis.contracts import PRODUCED_FEATURES
from wave_analysis.manifest import API_VERSION, PARAMETERS_SCHEMA


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def test_manifest_matches_public_module_contract() -> None:
    module = WaveAnalysisModule()
    manifest = module.manifest

    assert module.name == MODULE_NAME == "wave_analysis"
    assert manifest.name == module.name
    assert manifest.version == MODULE_VERSION == "0.2.0"
    assert manifest.api_version == API_VERSION == "1.0"
    assert manifest.state_schema_version == "1"
    assert manifest.produced_features == PRODUCED_FEATURES
    assert set(module.parameters) == set(PARAMETERS_SCHEMA)
    assert _thaw(manifest.parameters_schema) == PARAMETERS_SCHEMA
    assert manifest.required_features == ()
    assert manifest.optional_features == (module.config.optional_market_regime_feature,)
    assert manifest.skip_until_ready is False


def test_module_registry_accepts_default_and_multitimeframe_instances() -> None:
    registry = ModuleRegistry()
    for module, base_timeframe in (
        (WaveAnalysisModule(), "15m"),
        (
            WaveAnalysisModule(
                timeframes=("5m", "15m", "1h"),
                primary_timeframe="15m",
                pivot_method="hybrid_adaptive",
            ),
            "5m",
        ),
    ):
        validate_and_plan = getattr(registry, "validate_and_plan", None)
        if validate_and_plan is None:
            # The isolated RC harness uses a deliberately minimal Core compatibility wheel.
            # Repository CI exercises the full base-branch registry implementation.
            assert module.manifest.required_timeframes == module.config.timeframes
            continue
        plan = validate_and_plan((module,), base_timeframe=base_timeframe)
        assert plan.modules == (module,)
        assert plan.manifests == (module.manifest,)
        assert plan.produced_by == {feature: MODULE_NAME for feature in PRODUCED_FEATURES}
        expected = tuple(timeframe for timeframe in module.config.timeframes if timeframe != base_timeframe)
        assert plan.required_timeframes == expected


def test_parameter_schema_is_compatible_with_core_validator_types() -> None:
    supported = {"array", "string", "integer", "boolean"}
    assert PARAMETERS_SCHEMA
    for name, rules in PARAMETERS_SCHEMA.items():
        assert rules["type"] in supported, name
        assert "default" in rules, name
        if rules["type"] == "integer":
            assert isinstance(rules["minimum"], int)
            assert isinstance(rules["maximum"], int)
            assert rules["minimum"] <= rules["default"] <= rules["maximum"]
        if "enum" in rules:
            assert rules["default"] in rules["enum"]
