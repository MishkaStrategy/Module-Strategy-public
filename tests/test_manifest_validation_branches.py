from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from modular_core import (
    ModuleCompatibilityError,
    ModuleConfigurationError,
    ModuleDependencyError,
    ModuleManifest,
    ModuleRegistry,
    MarketView,
    ModuleDecision,
    StrategyModule,
)
from modular_core.manifest import (
    _matches_type,
    manifest_for,
    parameters_for,
    validate_parameters,
)


class _RegistryModule(StrategyModule):
    def __init__(
        self,
        name: str = "module",
        *,
        manifest: ModuleManifest | None = None,
        parameters: dict[str, Any] | None = None,
    ) -> None:
        self.name = name
        self.manifest = ModuleManifest(name=name) if manifest is None else manifest
        self.parameters = {} if parameters is None else parameters

    def on_bar(self, view: MarketView) -> ModuleDecision:
        del view
        return ModuleDecision()


def _module(
    manifest: ModuleManifest,
    *,
    name: str | None = None,
    parameters: dict[str, Any] | None = None,
) -> _RegistryModule:
    return _RegistryModule(
        name=manifest.name if name is None else name,
        manifest=manifest,
        parameters=parameters,
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"name": ""},
        {"name": "module", "version": ""},
        {"name": "module", "api_version": ""},
        {"name": "module", "state_schema_version": ""},
        {"name": "module", "required_features": ("unnamespaced",)},
        {"name": "module", "optional_features": ("",)},
        {"name": "module", "produced_features": ("bad",)},
        {"name": "module", "warmup_bars": {"": 1}},
        {"name": "module", "warmup_bars": {"5m": -1}},
    ],
)
def test_manifest_rejects_invalid_identity_and_schema(kwargs: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        ModuleManifest(**kwargs)


def test_manifest_freezes_nested_mappings() -> None:
    manifest = ModuleManifest(
        name="module",
        warmup_bars={"5m": 10},
        parameters_schema={"threshold": {"type": "number"}},
    )
    with pytest.raises(TypeError):
        manifest.warmup_bars["5m"] = 20  # type: ignore[index]
    with pytest.raises(TypeError):
        manifest.parameters_schema["threshold"]["type"] = "string"  # type: ignore[index]


def test_registry_rejects_blank_and_duplicate_registration() -> None:
    registry = ModuleRegistry()
    with pytest.raises(ValueError, match="registration name"):
        registry.register("", SimpleNamespace)
    registry.register("module", SimpleNamespace)
    with pytest.raises(ValueError, match="already registered"):
        registry.register("module", SimpleNamespace)


@pytest.mark.parametrize("path", ["invalid", ":Class", "package:"])
def test_import_path_requires_module_and_attribute(path: str) -> None:
    with pytest.raises(ModuleConfigurationError, match="module path"):
        ModuleRegistry().load_import_path(path)


def test_import_path_supports_colon_and_dotted_forms() -> None:
    registry = ModuleRegistry()
    colon = registry.load_import_path(f"{__name__}:_RegistryModule", {"name": "colon"})
    dotted = registry.load_import_path(f"{__name__}._RegistryModule", {"name": "dotted"})
    assert colon.name == "colon"
    assert dotted.name == "dotted"


def test_named_loading_covers_unknown_factory_and_instance_parameter_rejection() -> None:
    registry = ModuleRegistry()
    with pytest.raises(ModuleConfigurationError, match="unknown registered"):
        registry.load_named("missing")
    registry.register("factory", _RegistryModule)
    assert registry.load_named("factory", {"name": "loaded"}).name == "loaded"
    registry.register("instance", _RegistryModule(name="fixed"))
    assert registry.load_named("instance").name == "fixed"
    with pytest.raises(ModuleConfigurationError, match="parameters cannot"):
        registry.load_named("instance", {"name": "changed"})


def test_instantiation_wraps_constructor_type_errors() -> None:
    class RequiresValue:
        def __init__(self, value: str) -> None:
            self.value = value

    with pytest.raises(ModuleConfigurationError, match="cannot instantiate"):
        ModuleRegistry._instantiate(RequiresValue, {})


def test_load_config_supports_yaml_string_rows_disabled_rows_and_plugins(
    tmp_path: Path,
) -> None:
    registry = ModuleRegistry()
    registry.register("plugin", _RegistryModule)
    yaml_path = tmp_path / "modules.yml"
    yaml_path.write_text(
        "modules:\n"
        f"  - {__name__}._RegistryModule\n"
        "  - enabled: false\n"
        f"    class: {__name__}._RegistryModule\n"
        "  - plugin: plugin\n"
        "    parameters:\n"
        "      name: plugin-loaded\n",
        encoding="utf-8",
    )
    loaded = registry.load_config(yaml_path)
    assert len(loaded) == 2
    assert loaded[1].name == "plugin-loaded"


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"unknown": []}, "unknown module config keys"),
        ({"modules": "bad"}, "must be a sequence"),
        ({"modules": [42]}, "must be a mapping"),
        (
            {"modules": [{"class": "types.SimpleNamespace", "extra": True}]},
            "unknown keys",
        ),
        (
            {"modules": [{"class": "types.SimpleNamespace", "enabled": "yes"}]},
            "boolean",
        ),
        ({"modules": [{}]}, "exactly one"),
        (
            {
                "modules": [
                    {
                        "class": "types.SimpleNamespace",
                        "plugin": "plugin",
                    }
                ]
            },
            "exactly one",
        ),
        (
            {
                "modules": [
                    {
                        "class": "types.SimpleNamespace",
                        "parameters": "bad",
                    }
                ]
            },
            "parameters must be a mapping",
        ),
    ],
)
def test_load_config_rejects_malformed_shapes(
    config: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ModuleConfigurationError, match=message):
        ModuleRegistry().load_config(config)


def test_load_config_rejects_non_mapping_yaml_root(tmp_path: Path) -> None:
    path = tmp_path / "modules.yml"
    path.write_text("- not-a-mapping\n", encoding="utf-8")
    with pytest.raises(ModuleConfigurationError, match="root must be a mapping"):
        ModuleRegistry().load_config(path)


def test_planner_rejects_duplicate_names_and_name_mismatch() -> None:
    registry = ModuleRegistry()
    manifest = ModuleManifest(name="duplicate")
    with pytest.raises(ModuleCompatibilityError, match="names must be unique"):
        registry.validate_and_plan((_module(manifest), _module(manifest)), base_timeframe="5m")
    with pytest.raises(ModuleCompatibilityError, match="does not match"):
        registry.validate_and_plan(
            (_module(ModuleManifest(name="manifest-name"), name="runtime-name"),),
            base_timeframe="5m",
        )


def test_manifest_rejects_malformed_api_version() -> None:
    with pytest.raises(ValueError, match="MAJOR.MINOR"):
        ModuleManifest(name="module", api_version="invalid")


def test_planner_rejects_unsupported_api() -> None:
    module = _module(ModuleManifest(name="module", api_version="2.0"))
    with pytest.raises(ModuleCompatibilityError):
        ModuleRegistry().validate_and_plan((module,), base_timeframe="5m")


def test_planner_rejects_duplicate_missing_self_and_cyclic_dependencies() -> None:
    registry = ModuleRegistry()
    duplicate_a = _module(ModuleManifest(name="producer-a", produced_features=("shared.feature",)))
    duplicate_b = _module(ModuleManifest(name="producer-b", produced_features=("shared.feature",)))
    with pytest.raises(ModuleDependencyError, match="multiple producers"):
        registry.validate_and_plan((duplicate_a, duplicate_b), base_timeframe="5m")

    missing = _module(ModuleManifest(name="consumer", required_features=("missing.feature",)))
    with pytest.raises(ModuleDependencyError, match="missing module dependencies"):
        registry.validate_and_plan((missing,), base_timeframe="5m")

    with pytest.raises(ValueError, match="both required and produced"):
        ModuleManifest(
            name="self",
            required_features=("self.feature",),
            produced_features=("self.feature",),
        )

    cycle_a = _module(
        ModuleManifest(
            name="cycle-a",
            required_features=("cycle.b",),
            produced_features=("cycle.a",),
        )
    )
    cycle_b = _module(
        ModuleManifest(
            name="cycle-b",
            required_features=("cycle.a",),
            produced_features=("cycle.b",),
        )
    )
    with pytest.raises(ModuleDependencyError, match="cyclic"):
        registry.validate_and_plan((cycle_a, cycle_b), base_timeframe="5m")


def test_planner_orders_dependencies_and_deduplicates_required_timeframes() -> None:
    producer = _module(
        ModuleManifest(
            name="producer",
            required_timeframes=("5m", "1h"),
            produced_features=("producer.signal",),
            warmup_bars={"4h": 20},
        )
    )
    consumer = _module(
        ModuleManifest(
            name="consumer",
            required_timeframes=("1h", "15m"),
            required_features=("producer.signal",),
        )
    )
    plan = ModuleRegistry().validate_and_plan((consumer, producer), base_timeframe="5m")
    assert tuple(module.name for module in plan.modules) == ("producer", "consumer")
    assert plan.produced_by == {"producer.signal": "producer"}
    assert plan.required_timeframes == ("1h", "4h", "15m")


def test_manifest_and_parameters_helpers_cover_default_callable_and_invalid_paths() -> None:
    default = SimpleNamespace(name="default")
    assert manifest_for(default) == ModuleManifest(name="default")
    callable_manifest = SimpleNamespace(
        name="callable",
        manifest=lambda: ModuleManifest(name="callable"),
    )
    assert manifest_for(callable_manifest).name == "callable"
    with pytest.raises(ModuleCompatibilityError, match="must be ModuleManifest"):
        manifest_for(SimpleNamespace(name="bad", manifest="bad"))
    assert parameters_for(SimpleNamespace()) == {}
    with pytest.raises(ModuleConfigurationError, match="must be a mapping"):
        parameters_for(SimpleNamespace(parameters="bad"))


def _parameter_manifest() -> ModuleManifest:
    return ModuleManifest(
        name="parameterized",
        parameters_schema={
            "required": {"type": "integer", "required": True},
            "bounded": {"type": "number", "minimum": 0, "maximum": 10},
            "choice": {"type": "string", "enum": ("a", "b")},
            "defaulted": {"type": "boolean", "required": True, "default": True},
        },
    )


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"unknown": 1}, "unknown parameters"),
        ({}, "missing required"),
        ({"required": "1"}, "must be integer"),
        ({"required": 1, "bounded": -1}, "below minimum"),
        ({"required": 1, "bounded": 11}, "above maximum"),
        ({"required": 1, "choice": "c"}, "must be one of"),
    ],
)
def test_parameter_validation_rejects_schema_violations(
    parameters: dict[str, Any],
    message: str,
) -> None:
    with pytest.raises(ModuleConfigurationError, match=message):
        validate_parameters(_parameter_manifest(), parameters)


def test_parameter_validation_accepts_defaults_and_valid_values() -> None:
    validate_parameters(
        _parameter_manifest(),
        {"required": 1, "bounded": 2.5, "choice": "a"},
    )


@pytest.mark.parametrize(
    ("value", "expected", "matches"),
    [
        (1.5, "number", True),
        (True, "number", False),
        (1, "integer", True),
        (True, "integer", False),
        ("value", "string", True),
        (True, "boolean", True),
        ([1], "array", True),
        ((1,), "array", True),
        ({"key": "value"}, "object", True),
        (object(), "future-type", True),
    ],
)
def test_parameter_type_matching(value: Any, expected: str, matches: bool) -> None:
    assert _matches_type(value, expected) is matches
