from types import MappingProxyType

import pytest

from modular_core import ModuleManifest, ModuleRegistry
from modular_core.manifest import DependencyPlan, parameters_for


def test_manifest_copies_and_deep_freezes_caller_collections() -> None:
    timeframes = ["5m"]
    features = ["module.output"]
    enum_values = ["a", "b"]
    schema = {"mode": {"type": "string", "enum": enum_values}}
    manifest = ModuleManifest(
        name="module",
        required_timeframes=timeframes,  # type: ignore[arg-type]
        produced_features=features,  # type: ignore[arg-type]
        warmup_bars={"5m": 2},
        parameters_schema=schema,
    )
    timeframes.append("1h")
    features.append("module.late")
    enum_values.append("c")
    schema["late"] = {"type": "boolean"}
    assert manifest.required_timeframes == ("5m",)
    assert manifest.produced_features == ("module.output",)
    assert manifest.parameters_schema["mode"]["enum"] == ("a", "b")
    assert "late" not in manifest.parameters_schema
    assert isinstance(manifest.warmup_bars, MappingProxyType)


def test_dependency_plan_copies_inputs() -> None:
    manifest = ModuleManifest(name="module", produced_features=("module.output",))
    modules: list[object] = [object()]
    manifests = [manifest]
    produced_by = {"module.output": "module"}
    required = ["1h"]
    plan = DependencyPlan(modules, manifests, produced_by, required)  # type: ignore[arg-type]
    modules.clear()
    manifests.clear()
    produced_by.clear()
    required.clear()
    assert len(plan.modules) == 1
    assert plan.manifests == (manifest,)
    assert dict(plan.produced_by) == {"module.output": "module"}
    assert plan.required_timeframes == ("1h",)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"supported_api_major": True}, "supported_api_major must be int"),
        ({"max_config_bytes": True}, "max_config_bytes must be int"),
        ({"supported_api_major": 0}, "supported_api_major must be positive"),
        ({"max_config_bytes": 0}, "max_config_bytes must be positive"),
    ],
)
def test_registry_limit_types_are_strict(kwargs, message) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises((TypeError, ValueError), match=message):
        ModuleRegistry(**kwargs)


def test_module_parameters_are_snapshotted() -> None:
    class Module:
        parameters = {"nested": {"values": [1, 2]}}

    snapshot = parameters_for(Module())
    Module.parameters["nested"]["values"].append(3)
    assert snapshot["nested"]["values"] == (1, 2)
