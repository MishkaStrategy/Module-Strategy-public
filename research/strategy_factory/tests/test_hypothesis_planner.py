from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from conftest import module

from strategy_factory.contracts import Direction, ModuleCategory
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.grammar import StrategyGrammar
from strategy_factory.hypothesis import HypothesisGenerator, RegimeLibrary
from strategy_factory.planner import ArchitecturePlanner
from strategy_factory.registry import StrategyModuleCatalog


def populated_catalog() -> StrategyModuleCatalog:
    catalog = StrategyModuleCatalog()
    catalog.register(
        replace(
            module("fvg", ModuleCategory.SIGNAL, produced_features=("setup",)),
            parameter_schema={"minimum_gap_atr": {"minimum": "0.1", "maximum": "1.0"}},
            parameter_search_hints={"minimum_gap_atr": {"minimum": "0.1", "maximum": "1.0"}},
        )
    )
    catalog.register(
        replace(
            module("breakout", ModuleCategory.SIGNAL, produced_features=("setup",)),
            parameter_schema={"lookback": {"type": "integer", "minimum": 2, "maximum": 1000}},
            parameter_search_hints={"lookback": {"values": [24, 48, 96]}},
        )
    )
    catalog.register(
        replace(
            module("limit_retest", ModuleCategory.ENTRY, required_features=("setup",)),
            parameter_schema={"depth": {"minimum": "0", "maximum": "1"}},
            parameter_search_hints={"depth": {"minimum": "0", "maximum": "1", "factory_steps": 3}},
        )
    )
    catalog.register(module("atr_stop", ModuleCategory.PROTECTION))
    catalog.register(module("fixed_r", ModuleCategory.TARGET))
    catalog.register(module("fixed_fractional", ModuleCategory.RISK))
    return catalog


def test_regime_library_is_point_in_time() -> None:
    regimes = RegimeLibrary.default(("5m", "1h"))
    assert len(regimes) == 10
    assert all(item.point_in_time for item in regimes)
    assert {item.regime_id for item in regimes} >= {"uptrend", "crisis", "insufficient_liquidity"}


def test_hypothesis_and_architecture_planning(factory_request) -> None:
    profile = (
        DatasetAnalyzer(
            DatasetValidationPolicy(
                interval_seconds=300,
                minimum_rows=10,
                minimum_history_days=Decimal("0"),
            )
        )
        .require_accepted(factory_request.dataset)
        .profile
    )
    assert profile is not None
    catalog = populated_catalog()
    hypotheses = HypothesisGenerator(catalog).generate(
        request=factory_request,
        profile=profile,
    )
    assert hypotheses
    assert all(item.falsification_criteria["holdout_not_used_for_selection"] for item in hypotheses)
    planner = ArchitecturePlanner(StrategyGrammar(catalog))
    generated = planner.generate_architectures(
        request=factory_request,
        hypotheses=hypotheses,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
    )
    assert generated.architectures
    space = planner.build_parameter_space(
        generated.architectures[0],
        maximum_combinations=100,
    )
    assert space.maximum_combinations <= 100
    assert all("." in key for key in space.dimensions)


def test_parameter_space_has_deterministic_noop_for_fixed_architecture(architecture) -> None:
    from dataclasses import replace

    fixed = replace(
        architecture,
        target=replace(architecture.target, fixed_parameters={"reward_r": 4}),
        risk=replace(
            architecture.risk,
            fixed_parameters={"risk_fraction": "0.005"},
        ),
    )
    planner = ArchitecturePlanner(StrategyGrammar(StrategyModuleCatalog()))
    space = planner.build_parameter_space(fixed, maximum_combinations=10)
    assert space.dimensions == {"factory.noop_variant": (0,)}
    assert space.maximum_combinations == 1


def test_parameter_space_deduplicates_shared_runtime_component(descriptor) -> None:
    from strategy_factory.contracts import StrategyArchitecture

    roles = frozenset({ModuleCategory.PROTECTION, ModuleCategory.TARGET})
    shared = {
        "required_features": ("entry_plan",),
        "produced_features": ("protection_plan",),
        "import_path": "modular_strategy_protection_target:ProtectionTargetModule",
        "runtime_component_id": "protection_target",
        "runtime_component_version": "1.1.1",
        "runtime_role_categories": roles,
        "parameter_schema": {"reward_r": {"minimum": 0, "maximum": 20}},
        "parameter_search_hints": {"reward_r": {"values": [2, 3, 4]}},
    }
    architecture = StrategyArchitecture(
        architecture_id="arch-shared-runtime",
        market_type=descriptor.market_type,
        directions=frozenset({Direction.LONG}),
        signal=module("fvg", ModuleCategory.SIGNAL, produced_features=("setup",)),
        context_filters=(),
        entry=module(
            "entry",
            ModuleCategory.ENTRY,
            required_features=("setup",),
            produced_features=("entry_plan",),
        ),
        protection=module(
            "protection_target.protection",
            ModuleCategory.PROTECTION,
            role_facet="protection",
            **shared,
        ),
        target=module(
            "protection_target.target",
            ModuleCategory.TARGET,
            role_facet="target",
            **shared,
        ),
        risk=module(
            "risk",
            ModuleCategory.RISK,
            required_features=("protection_plan",),
        ),
    )
    space = ArchitecturePlanner(StrategyGrammar(StrategyModuleCatalog())).build_parameter_space(
        architecture,
        maximum_combinations=100,
    )
    assert space.dimensions == {"protection_target.reward_r": (2, 3, 4)}
