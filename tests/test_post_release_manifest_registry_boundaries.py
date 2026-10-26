from __future__ import annotations

import pytest

from modular_core import (
    MarketView,
    ModuleConfigurationError,
    ModuleDecision,
    ModuleManifest,
    ModuleRegistry,
    StrategyModule,
)
from modular_core.manifest import DependencyPlan


class _ValidModule(StrategyModule):
    name = "valid"
    manifest = ModuleManifest(name=name, produced_features=("valid.output",))

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


class _MismatchedModule(StrategyModule):
    name = "runtime_name"
    manifest = ModuleManifest(name="manifest_name")

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision()


def test_manifest_rejects_unknown_parameter_schema_rules() -> None:
    with pytest.raises(ValueError, match="unknown parameter schema rules"):
        ModuleManifest(
            name="probe",
            parameters_schema={
                "period": {
                    "type": "integer",
                    "minimum": 1,
                    "mininum": 2,
                }
            },
        )


def test_dependency_plan_requires_producer_evidence_from_manifests() -> None:
    manifest = ModuleManifest(
        name="producer",
        produced_features=("producer.output",),
    )

    valid = DependencyPlan(
        modules=(object(),),
        manifests=(manifest,),
        produced_by={"producer.output": "producer"},
        required_timeframes=(),
    )
    assert dict(valid.produced_by) == {"producer.output": "producer"}

    with pytest.raises(ValueError, match="produced_by is inconsistent"):
        DependencyPlan(
            modules=(object(),),
            manifests=(manifest,),
            produced_by={},
            required_timeframes=(),
        )
    with pytest.raises(ValueError, match="produced_by is inconsistent"):
        DependencyPlan(
            modules=(object(),),
            manifests=(manifest,),
            produced_by={"producer.output": "other"},
            required_timeframes=(),
        )
    with pytest.raises(ValueError, match="produced_by is inconsistent"):
        DependencyPlan(
            modules=(object(),),
            manifests=(manifest,),
            produced_by={
                "producer.output": "producer",
                "forged.output": "producer",
            },
            required_timeframes=(),
        )


def test_dependency_plan_validates_named_module_manifest_alignment() -> None:
    with pytest.raises(ValueError, match="does not match manifest.name"):
        DependencyPlan(
            modules=(_MismatchedModule(),),
            manifests=(_MismatchedModule.manifest,),
            produced_by={},
            required_timeframes=(),
        )


def test_registry_rejects_arbitrary_registered_object_when_loaded() -> None:
    registry = ModuleRegistry()
    registry.register("invalid", object())

    with pytest.raises(
        ModuleConfigurationError,
        match="must implement StrategyModule",
    ):
        registry.load_named("invalid")


def test_registry_rejects_invalid_factory_result() -> None:
    registry = ModuleRegistry()
    registry.register("invalid", lambda: object())

    with pytest.raises(
        ModuleConfigurationError,
        match="must implement StrategyModule",
    ):
        registry.load_named("invalid")


def test_registry_accepts_valid_factory_and_instance_aliases() -> None:
    factory_registry = ModuleRegistry()
    factory_registry.register("factory_alias", _ValidModule)
    factory_module = factory_registry.load_named("factory_alias")
    assert isinstance(factory_module, _ValidModule)

    instance = _ValidModule()
    instance_registry = ModuleRegistry()
    instance_registry.register("instance_alias", instance)
    assert instance_registry.load_named("instance_alias") is instance


def test_registry_rejects_loaded_module_manifest_name_mismatch() -> None:
    registry = ModuleRegistry()
    registry.register("mismatch", _MismatchedModule)

    with pytest.raises(
        ModuleConfigurationError,
        match="does not match manifest.name",
    ):
        registry.load_named("mismatch")
