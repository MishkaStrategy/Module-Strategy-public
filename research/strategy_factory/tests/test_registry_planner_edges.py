from __future__ import annotations

import copy
from dataclasses import replace
from decimal import Decimal
from types import SimpleNamespace

import pytest
from conftest import module

from strategy_factory.contracts import (
    Direction,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
    StrategyHypothesis,
)
from strategy_factory.errors import CompatibilityError, ContractError
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.planner import (
    ArchitecturePlanner,
    _bound_dimensions,
    _evenly_spaced_prefix_preserving,
    _local_seeded_values,
    _parameter_values,
)
from strategy_factory.registry import (
    ModuleRegistrationMetadata,
    StrategyModuleCatalog,
    _version_key,
)


def _metadata(**changes: object) -> ModuleRegistrationMetadata:
    values: dict[str, object] = {
        "category": ModuleCategory.SIGNAL,
        "import_path": "fixture:core-module",
        "required_data": frozenset({"ohlcv"}),
        "supported_markets": frozenset({MarketType.LINEAR_FUTURES}),
        "supported_directions": frozenset({Direction.LONG}),
        "deterministic": True,
        "test_coverage": Decimal("95"),
        "maturity": ModuleMaturity.PAPER_READY,
    }
    values.update(changes)
    return ModuleRegistrationMetadata(**values)  # type: ignore[arg-type]


def _manifest(**changes: object):
    values = {
        "name": "core_signal",
        "version": "1.2.3",
        "api_version": "1.0",
        "required_timeframes": ("5m",),
        "required_features": (),
        "produced_features": ("setup",),
        "optional_features": ("diagnostic",),
        "parameters_schema": {"period": {"type": "integer", "minimum": 1}},
        "state_schema_version": "1",
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_catalog_register_is_idempotent_and_rejects_conflicting_identity() -> None:
    catalog = StrategyModuleCatalog()
    reference = module("signal", ModuleCategory.SIGNAL)
    catalog.register(reference)
    catalog.register(reference)
    conflict = replace(reference, import_path="fixture:other")
    with pytest.raises(ContractError, match="conflicting registration"):
        catalog.register(conflict)


def test_catalog_adapts_core_manifest_and_role_metadata() -> None:
    catalog = StrategyModuleCatalog()
    reference = catalog.register_core_manifest(
        _manifest(),
        _metadata(
            role_id="logical_signal",
            fixed_parameters={"period": 5},
            evidence_links=("evidence",),
            source_commit_sha="a" * 40,
        ),
    )
    assert reference.module_id == "logical_signal"
    assert reference.runtime_component_id == "core_signal"
    assert reference.fixed_parameters == {"period": 5}
    assert reference.optional_features == ("diagnostic",)
    assert catalog.get("logical_signal", "1.2.3") == reference


def test_catalog_rejects_incomplete_core_manifest() -> None:
    manifest = _manifest()
    del manifest.parameters_schema
    with pytest.raises(CompatibilityError, match="missing fields: parameters_schema"):
        StrategyModuleCatalog().register_core_manifest(manifest, _metadata())


def test_catalog_register_module_instance_validates_manifest_and_parameters() -> None:
    catalog = StrategyModuleCatalog()
    with pytest.raises(CompatibilityError, match="no manifest"):
        catalog.register_module_instance(SimpleNamespace(), _metadata())
    with pytest.raises(CompatibilityError, match="must be a mapping"):
        catalog.register_module_instance(
            SimpleNamespace(manifest=_manifest(), parameters=[]),
            _metadata(),
        )
    with pytest.raises(CompatibilityError, match="conflicts with registration"):
        catalog.register_module_instance(
            SimpleNamespace(manifest=_manifest(), parameters={"period": 4}),
            _metadata(fixed_parameters={"period": 5}),
        )
    registered = catalog.register_module_instance(
        SimpleNamespace(manifest=_manifest(), parameters={"period": 5}),
        _metadata(fixed_parameters={"period": 5}),
    )
    assert registered.fixed_parameters == {"period": 5}


def test_catalog_queries_are_deterministic_and_fail_closed() -> None:
    catalog = StrategyModuleCatalog()
    versions = (
        replace(module("signal", ModuleCategory.SIGNAL), version="1.9.0"),
        replace(module("signal", ModuleCategory.SIGNAL), version="1.10.0"),
        replace(module("entry", ModuleCategory.ENTRY), version="2.0.0"),
    )
    for item in reversed(versions):
        catalog.register(item)
    assert catalog.get("signal").version == "1.10.0"
    assert [item.module_id for item in catalog.by_category(ModuleCategory.SIGNAL)] == [
        "signal",
        "signal",
    ]
    assert [item.key for item in catalog.all()] == sorted(item.key for item in versions)
    snapshot = catalog.snapshot()
    with pytest.raises(TypeError):
        snapshot["new"] = versions[0]
    assert catalog.require((versions[0].key, versions[2].key)) == (
        versions[0],
        versions[2],
    )
    with pytest.raises(CompatibilityError, match="unknown module"):
        catalog.get("missing")
    with pytest.raises(CompatibilityError, match="unknown module version"):
        catalog.get("signal", "9.9.9")
    with pytest.raises(CompatibilityError, match="required module is unavailable"):
        catalog.require(("missing@1.0.0",))


def test_version_key_handles_suffix_and_non_numeric_versions() -> None:
    assert _version_key("1.10.0") > _version_key("1.9.0")
    assert _version_key("1.0.0-rc1") == ((1, 0, 0), "rc1")
    assert _version_key("development") == ((), "development")


def test_architecture_planner_rejects_hypotheses_without_signal(factory_request) -> None:
    hypothesis = StrategyHypothesis(
        hypothesis_id="hypothesis-empty",
        description="No eligible modules",
        expected_edge="none",
        applicability=("all",),
        risks=("none",),
        module_candidates=(),
        parameter_ranges={},
        falsification_criteria={"reject": True},
        estimated_experiments=1,
        novelty=Decimal("0"),
        origin={"type": "test"},
    )
    planner = ArchitecturePlanner(StrategyGrammar(StrategyModuleCatalog()))
    with pytest.raises(CompatibilityError, match="no hypothesis"):
        planner.generate_architectures(
            request=factory_request,
            hypotheses=(hypothesis,),
            directions=frozenset({Direction.LONG}),
        )


def test_parameter_space_rejects_invalid_schema_hint_and_constraints(architecture) -> None:
    planner = ArchitecturePlanner(StrategyGrammar(StrategyModuleCatalog()))

    invalid_schema = copy.copy(architecture.target)
    object.__setattr__(invalid_schema, "parameter_schema", {"reward_r": "invalid"})
    object.__setattr__(
        invalid_schema,
        "parameter_search_hints",
        {"reward_r": {"values": (2, 3)}},
    )
    with pytest.raises(ContractError, match="schema/search hint"):
        planner.build_parameter_space(
            replace(architecture, target=invalid_schema), maximum_combinations=10
        )

    no_values = copy.copy(architecture.target)
    object.__setattr__(no_values, "parameter_schema", {"reward_r": {"type": "number"}})
    object.__setattr__(no_values, "parameter_search_hints", {"reward_r": {}})
    with pytest.raises(ContractError, match="has no bounded values"):
        planner.build_parameter_space(
            replace(architecture, target=no_values), maximum_combinations=10
        )

    bad_constraints = copy.copy(architecture.target)
    object.__setattr__(
        bad_constraints,
        "parameter_schema",
        {"reward_r": {"values": [2, 3]}},
    )
    object.__setattr__(
        bad_constraints,
        "parameter_search_hints",
        {"reward_r": {"values": [2, 3], "constraints": "invalid"}},
    )
    with pytest.raises(ContractError, match="constraints must be a sequence"):
        planner.build_parameter_space(
            replace(architecture, target=bad_constraints), maximum_combinations=10
        )

    non_mapping_constraint = copy.copy(architecture.target)
    object.__setattr__(
        non_mapping_constraint,
        "parameter_schema",
        {"reward_r": {"values": [2, 3]}},
    )
    object.__setattr__(
        non_mapping_constraint,
        "parameter_search_hints",
        {"reward_r": {"values": [2, 3], "constraints": [1]}},
    )
    with pytest.raises(ContractError, match="constraint must be an object"):
        planner.build_parameter_space(
            replace(architecture, target=non_mapping_constraint),
            maximum_combinations=10,
        )


def test_seeded_parameter_space_validation_and_frozen_mode(architecture) -> None:
    searchable_target = replace(
        architecture.target,
        parameter_search_hints={"reward_r": {"values": (2, 4, 6)}},
    )
    architecture = replace(architecture, target=searchable_target)
    planner = ArchitecturePlanner(StrategyGrammar(StrategyModuleCatalog()))
    for seed in ({}, [], None):
        with pytest.raises(ContractError, match="non-empty mapping"):
            planner.build_seeded_parameter_space(
                architecture,
                seed_parameters=seed,  # type: ignore[arg-type]
                maximum_combinations=10,
                allow_adaptation=True,
            )
    with pytest.raises(ContractError, match="must be boolean"):
        planner.build_seeded_parameter_space(
            architecture,
            seed_parameters={"fixed_r.reward_r": 4},
            maximum_combinations=10,
            allow_adaptation=1,  # type: ignore[arg-type]
        )
    for points in (True, 0, 22, "3"):
        with pytest.raises(ContractError, match="neighborhood_points"):
            planner.build_seeded_parameter_space(
                architecture,
                seed_parameters={"fixed_r.reward_r": 4},
                maximum_combinations=10,
                allow_adaptation=True,
                neighborhood_points=points,  # type: ignore[arg-type]
            )
    with pytest.raises(CompatibilityError, match="unknown runtime fields"):
        planner.build_seeded_parameter_space(
            architecture,
            seed_parameters={"unknown.value": 1},
            maximum_combinations=10,
            allow_adaptation=True,
        )
    with pytest.raises(CompatibilityError, match="absent from architecture"):
        planner.build_seeded_parameter_space(
            architecture,
            seed_parameters={"fixed_r.reward_r": 4},
            maximum_combinations=10,
            allow_adaptation=True,
            allowed_parameter_names=frozenset({"unknown.value"}),
        )
    frozen = planner.build_seeded_parameter_space(
        architecture,
        seed_parameters={"fixed_r.reward_r": 4},
        maximum_combinations=10,
        allow_adaptation=False,
    )
    assert frozen.dimensions == {"fixed_r.reward_r": (4,)}


def test_seeded_parameter_space_requires_all_searchable_values(architecture) -> None:
    target = replace(
        architecture.target,
        parameter_search_hints={"reward_r": {"values": (2, 4, 6)}},
    )
    risk = replace(
        architecture.risk,
        parameter_search_hints={"risk_fraction": {"values": ("0.001", "0.005")}},
    )
    architecture = replace(architecture, target=target, risk=risk)
    with pytest.raises(CompatibilityError, match="has no value"):
        ArchitecturePlanner(StrategyGrammar(StrategyModuleCatalog())).build_seeded_parameter_space(
            architecture,
            seed_parameters={"fixed_r.reward_r": 4},
            maximum_combinations=10,
            allow_adaptation=True,
        )


@pytest.mark.parametrize(
    ("schema", "expected"),
    [
        ({"values": [1, 2]}, (1, 2)),
        ({"enum": ("a", "b")}, ("a", "b")),
        ({"default": 7}, (7,)),
        ({"minimum": 2, "maximum": 2}, (2,)),
        ({"minimum": "1.0", "maximum": "1.0"}, (Decimal("1.0"),)),
        ({"type": "string"}, None),
    ],
)
def test_parameter_values_supported_shapes(schema, expected) -> None:
    assert _parameter_values(schema) == expected


@pytest.mark.parametrize(
    ("schema", "message"),
    [
        ({"values": []}, "values must be"),
        ({"values": "bad"}, "values must be"),
        ({"enum": []}, "enum must be"),
        ({"minimum": 1, "maximum": 3, "factory_steps": True}, "factory_steps"),
        ({"minimum": 3, "maximum": 1}, "maximum is below"),
        ({"minimum": object(), "maximum": 1}, "bounds are invalid"),
        ({"minimum": "NaN", "maximum": 1}, "bounds are invalid"),
    ],
)
def test_parameter_values_rejects_invalid_shapes(schema, message: str) -> None:
    with pytest.raises(ContractError, match=message):
        _parameter_values(schema)


def test_parameter_values_generates_integer_and_decimal_grids() -> None:
    assert _parameter_values({"minimum": 0, "maximum": 10, "factory_steps": 3}) == (
        0,
        5,
        10,
    )
    assert _parameter_values({"minimum": "0", "maximum": "1", "factory_steps": 3}) == (
        Decimal("0"),
        Decimal("0.5"),
        Decimal("1"),
    )


def test_local_seeded_values_handles_strings_booleans_numbers_and_fallback() -> None:
    assert _local_seeded_values(("a", "b", "c"), "b", 2)[0] == "b"
    assert _local_seeded_values((False, True, 1), True, 2)[0] is True
    integers = _local_seeded_values((1, "2", "bad", 4), 2, 3)
    assert integers[0] == 2
    fallback = _local_seeded_values(("x", "y"), "seed", 2)
    assert len(fallback) == 2
    assert _local_seeded_values((1, 2, 3), 99, 1) == (99,)


def test_dimension_bounding_and_spacing_fail_closed() -> None:
    with pytest.raises(ContractError, match="positive int"):
        _bound_dimensions({"a": (1, 2)}, 0)
    bounded = _bound_dimensions({"a": (1, 2, 3), "b": (4, 5, 6)}, 4)
    assert len(bounded["a"]) * len(bounded["b"]) <= 4
    assert _bound_dimensions({"a": (), "b": ()}, 1) == {"a": (), "b": ()}

    values = (0, 1, 2, 3, 4)
    assert _evenly_spaced_prefix_preserving(values, 6) == values
    assert _evenly_spaced_prefix_preserving(values, 1) == (0,)
    assert _evenly_spaced_prefix_preserving(values, 2) == (0, 3)
    assert _evenly_spaced_prefix_preserving((0, 1), 2) == (0, 1)
    assert len(_evenly_spaced_prefix_preserving(values, 4)) == 4
