from __future__ import annotations

import pytest
from conftest import module

from strategy_factory.contracts import Direction, ModuleCategory, ModuleMaturity
from strategy_factory.errors import CompatibilityError
from strategy_factory.grammar import GrammarPolicy, StrategyGrammar
from strategy_factory.registry import StrategyModuleCatalog


def catalog() -> StrategyModuleCatalog:
    result = StrategyModuleCatalog()
    for item in (
        module("fvg", ModuleCategory.SIGNAL, produced_features=("setup",)),
        module("ema_filter", ModuleCategory.CONTEXT_FILTER),
        module("limit_retest", ModuleCategory.ENTRY, required_features=("setup",)),
        module("atr_stop", ModuleCategory.PROTECTION),
        module("fixed_r", ModuleCategory.TARGET),
        module("fixed_fractional", ModuleCategory.RISK),
        module("max_hold", ModuleCategory.POSITION_MANAGEMENT),
    ):
        result.register(item)
    return result


def test_deterministic_architecture_generation(descriptor) -> None:
    grammar = StrategyGrammar(catalog())
    first = grammar.generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
        maximum_architectures=20,
    )
    second = grammar.generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
        maximum_architectures=20,
    )
    assert [item.fingerprint for item in first.architectures] == [
        item.fingerprint for item in second.architectures
    ]
    assert len(first.architectures) == 4


def test_missing_funding_module_is_not_eligible(descriptor) -> None:
    value = catalog()
    value.register(
        module(
            "funding_filter",
            ModuleCategory.CONTEXT_FILTER,
            required_data=frozenset({"funding"}),
        )
    )
    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=20,
    )
    assert all(
        "funding_filter" not in {item.module_id for item in architecture.modules}
        for architecture in generated.architectures
    )


def test_conflict_tags_reject_combination(descriptor) -> None:
    value = catalog()
    value.register(
        module(
            "market_entry",
            ModuleCategory.ENTRY,
            conflict_tags=frozenset({"limit-only"}),
        )
    )
    value.register(
        module(
            "limit_context",
            ModuleCategory.CONTEXT_FILTER,
            compatibility_tags=frozenset({"limit-only"}),
        )
    )
    result = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=100,
    )
    assert any("CONFLICTING_MODULE_TAGS" in item.reason_codes for item in result.rejections)


def test_final_candidate_rejects_experimental_module(descriptor) -> None:
    value = catalog()
    value.register(
        module(
            "experimental_signal",
            ModuleCategory.SIGNAL,
            maturity=ModuleMaturity.EXPERIMENTAL,
            coverage="100",
        )
    )
    result = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=100,
        final_candidate=True,
    )
    assert all(
        architecture.signal.module_id != "experimental_signal"
        for architecture in result.architectures
    )


def test_final_candidate_rejects_low_coverage(descriptor) -> None:
    value = catalog()
    value.register(module("weak_signal", ModuleCategory.SIGNAL, coverage="50"))
    grammar = StrategyGrammar(value, GrammarPolicy(minimum_final_coverage=90))
    result = grammar.generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        final_candidate=True,
    )
    assert all(
        architecture.signal.module_id != "weak_signal" for architecture in result.architectures
    )


def test_missing_required_category_fails(descriptor) -> None:
    value = StrategyModuleCatalog()
    value.register(module("fvg", ModuleCategory.SIGNAL))
    with pytest.raises(CompatibilityError, match="no eligible modules"):
        StrategyGrammar(value).generate(
            dataset=descriptor,
            directions=frozenset({Direction.LONG}),
        )


def test_catalog_conflicting_duplicate_rejected() -> None:
    value = StrategyModuleCatalog()
    original = module("fvg", ModuleCategory.SIGNAL)
    value.register(original)
    changed = module("fvg", ModuleCategory.SIGNAL, coverage="90")
    with pytest.raises(Exception, match="conflicting registration"):
        value.register(changed)


def _shared_protection_target_refs(*, conflicting: bool = False):
    roles = frozenset({ModuleCategory.PROTECTION, ModuleCategory.TARGET})
    common = {
        "required_features": ("entry_plan",),
        "produced_features": ("protection_plan",),
        "import_path": "modular_strategy_protection_target:ProtectionTargetModule",
        "runtime_component_id": "protection_target",
        "runtime_component_version": "1.1.1",
        "runtime_role_categories": roles,
        "parameter_schema": {"reward_r": {"minimum": 1, "maximum": 5}},
    }
    protection = module(
        "protection_target.protection",
        ModuleCategory.PROTECTION,
        role_facet="protection",
        **common,
    )
    target_schema = (
        {"reward_r": {"minimum": 2, "maximum": 8}} if conflicting else common["parameter_schema"]
    )
    target = module(
        "protection_target.target",
        ModuleCategory.TARGET,
        role_facet="target",
        **{**common, "parameter_schema": target_schema},
    )
    return protection, target


def test_shared_runtime_component_fills_protection_and_target_once(descriptor) -> None:
    value = StrategyModuleCatalog()
    protection, target = _shared_protection_target_refs()
    for item in (
        module("fvg", ModuleCategory.SIGNAL, produced_features=("setup",)),
        module(
            "limit_retest",
            ModuleCategory.ENTRY,
            required_features=("setup",),
            produced_features=("entry_plan",),
        ),
        protection,
        target,
        module("fixed_fractional", ModuleCategory.RISK, required_features=("protection_plan",)),
    ):
        value.register(item)
    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
    )
    assert len(generated.architectures) == 1
    architecture = generated.architectures[0]
    assert len(architecture.modules) == 5
    assert len(architecture.runtime_modules) == 4
    assert [item.runtime_key for item in architecture.runtime_modules].count(
        "protection_target@1.1.1"
    ) == 1


def test_shared_runtime_component_conflicting_contract_is_rejected(descriptor) -> None:
    value = StrategyModuleCatalog()
    protection, target = _shared_protection_target_refs(conflicting=True)
    for item in (
        module("fvg", ModuleCategory.SIGNAL, produced_features=("setup",)),
        module(
            "limit_retest",
            ModuleCategory.ENTRY,
            required_features=("setup",),
            produced_features=("entry_plan",),
        ),
        protection,
        target,
        module("fixed_fractional", ModuleCategory.RISK, required_features=("protection_plan",)),
    ):
        value.register(item)
    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
    )
    assert not generated.architectures
    assert any(
        "RUNTIME_COMPONENT_CONTRACT_CONFLICT" in rejection.reason_codes
        for rejection in generated.rejections
    )


def test_setup_adapter_is_required_for_real_feature_chain(descriptor) -> None:
    value = StrategyModuleCatalog()
    for item in (
        module(
            "breakout_return",
            ModuleCategory.SIGNAL,
            produced_features=("breakout_return.confirmed",),
        ),
        module(
            "setup_adapter.breakout_return_v1",
            ModuleCategory.SETUP_ADAPTER,
            required_features=("breakout_return.confirmed",),
            produced_features=("setup.confirmed",),
        ),
        module(
            "entry_model",
            ModuleCategory.ENTRY,
            required_features=("setup.confirmed",),
            produced_features=("entry_model.plan",),
        ),
        module(
            "protection",
            ModuleCategory.PROTECTION,
            required_features=("entry_model.plan",),
            produced_features=("protection.plan",),
        ),
        module(
            "target",
            ModuleCategory.TARGET,
            required_features=("entry_model.plan",),
            produced_features=("target.plan",),
        ),
        module(
            "risk",
            ModuleCategory.RISK,
            required_features=("protection.plan",),
            produced_features=("risk.decision",),
        ),
    ):
        value.register(item)
    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
    )
    assert len(generated.architectures) == 1
    assert generated.architectures[0].setup_adapters[0].module_id == (
        "setup_adapter.breakout_return_v1"
    )
    assert any(
        "MISSING_FEATURE_DEPENDENCY" in rejection.reason_codes for rejection in generated.rejections
    )


def test_dependency_cycle_is_rejected(descriptor) -> None:
    value = StrategyModuleCatalog()
    for item in (
        module(
            "signal",
            ModuleCategory.SIGNAL,
            required_features=("filter.output",),
            produced_features=("signal.output",),
        ),
        module(
            "filter",
            ModuleCategory.CONTEXT_FILTER,
            required_features=("signal.output",),
            produced_features=("filter.output",),
        ),
        module("entry", ModuleCategory.ENTRY),
        module("protection", ModuleCategory.PROTECTION),
        module("target", ModuleCategory.TARGET),
        module("risk", ModuleCategory.RISK),
    ):
        value.register(item)
    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
    )
    assert not generated.architectures
    assert any(
        "FEATURE_DEPENDENCY_CYCLE" in rejection.reason_codes for rejection in generated.rejections
    )


def test_duplicate_feature_producers_are_rejected(descriptor) -> None:
    value = catalog()
    value.register(
        module(
            "duplicate_setup_filter",
            ModuleCategory.CONTEXT_FILTER,
            produced_features=("setup",),
        )
    )
    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=100,
    )
    assert any(
        "DUPLICATE_FEATURE_PRODUCER" in rejection.reason_codes for rejection in generated.rejections
    )


def test_risk_request_adapter_closes_real_sizing_dependency_chain(descriptor) -> None:
    value = StrategyModuleCatalog()
    for item in (
        module("signal", ModuleCategory.SIGNAL, produced_features=("setup.confirmed",)),
        module(
            "entry",
            ModuleCategory.ENTRY,
            required_features=("setup.confirmed",),
            produced_features=("entry_model.plan",),
        ),
        module(
            "protection",
            ModuleCategory.PROTECTION,
            required_features=("entry_model.plan",),
            produced_features=("protection_target.plan",),
        ),
        module("target", ModuleCategory.TARGET),
        module(
            "risk_request_composer",
            ModuleCategory.RISK_REQUEST_ADAPTER,
            required_features=(
                "entry_model.plan",
                "protection_target.plan",
                "instrument.rules",
                "portfolio.risk_snapshot",
            ),
            produced_features=("risk.request",),
            required_data=frozenset({"ohlcv", "instrument_rules", "portfolio_snapshot"}),
        ),
        module(
            "risk_sizing",
            ModuleCategory.RISK,
            required_features=("risk.request",),
            produced_features=("risk.decision",),
        ),
    ):
        value.register(item)
    grammar = StrategyGrammar(value)
    blocked = grammar.generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
    )
    assert not blocked.architectures
    accepted = grammar.generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        external_features=frozenset({"instrument.rules", "portfolio.risk_snapshot"}),
        runtime_data=frozenset({"instrument_rules", "portfolio_snapshot"}),
    )
    assert len(accepted.architectures) == 1
    assert accepted.architectures[0].risk_adapters[0].module_id == ("risk_request_composer")


def test_conflicting_configuration_for_same_runtime_is_rejected(descriptor) -> None:
    value = StrategyModuleCatalog()
    for item in (
        module(
            "signal",
            ModuleCategory.SIGNAL,
            produced_features=("signal.a", "signal.b"),
        ),
        module(
            "adapter.a",
            ModuleCategory.SETUP_ADAPTER,
            required_features=("signal.a",),
            produced_features=("setup.a",),
            runtime_component_id="setup_adapter",
            runtime_component_version="0.1.0",
            parameter_schema={"sources": {"type": "array"}},
            fixed_parameters={"sources": ({"profile": "a"},)},
        ),
        module(
            "adapter.b",
            ModuleCategory.SETUP_ADAPTER,
            required_features=("signal.b",),
            produced_features=("setup.b",),
            runtime_component_id="setup_adapter",
            runtime_component_version="0.1.0",
            parameter_schema={"sources": {"type": "array"}},
            fixed_parameters={"sources": ({"profile": "b"},)},
        ),
        module("entry", ModuleCategory.ENTRY),
        module("protection", ModuleCategory.PROTECTION),
        module("target", ModuleCategory.TARGET),
        module("risk", ModuleCategory.RISK),
    ):
        value.register(item)

    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=100,
    )

    assert any(
        "RUNTIME_COMPONENT_CONFIGURATION_CONFLICT" in rejection.reason_codes
        for rejection in generated.rejections
    )
    assert all(len(architecture.setup_adapters) < 2 for architecture in generated.architectures)


def test_liquidity_sweep_selects_matching_setup_adapter(descriptor) -> None:
    value = StrategyModuleCatalog()
    for item in (
        module(
            "liquidity_sweep",
            ModuleCategory.SIGNAL,
            produced_features=("liquidity_sweep.event",),
        ),
        module(
            "setup_adapter.breakout_return_v1",
            ModuleCategory.SETUP_ADAPTER,
            required_features=("breakout_return.confirmed",),
            produced_features=("setup.confirmed",),
            runtime_component_id="setup_adapter",
            runtime_component_version="0.1.0",
            parameter_schema={"sources": {"type": "array"}},
            fixed_parameters={"sources": ({"profile": "breakout_return_v1"},)},
        ),
        module(
            "setup_adapter.liquidity_sweep_v1",
            ModuleCategory.SETUP_ADAPTER,
            required_features=("liquidity_sweep.event",),
            produced_features=("setup.confirmed",),
            runtime_component_id="setup_adapter",
            runtime_component_version="0.1.0",
            parameter_schema={"sources": {"type": "array"}},
            fixed_parameters={"sources": ({"profile": "liquidity_sweep_v1"},)},
        ),
        module(
            "entry",
            ModuleCategory.ENTRY,
            required_features=("setup.confirmed",),
            produced_features=("entry.plan",),
        ),
        module("protection", ModuleCategory.PROTECTION),
        module("target", ModuleCategory.TARGET),
        module("risk", ModuleCategory.RISK),
    ):
        value.register(item)

    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=100,
    )

    assert len(generated.architectures) == 1
    architecture = generated.architectures[0]
    assert architecture.signal.module_id == "liquidity_sweep"
    assert tuple(item.module_id for item in architecture.setup_adapters) == (
        "setup_adapter.liquidity_sweep_v1",
    )


def test_fvg_rt_composite_selects_matching_three_source_setup_adapter(descriptor) -> None:
    value = StrategyModuleCatalog()
    for item in (
        module(
            "fvg_rt",
            ModuleCategory.SIGNAL,
            produced_features=(
                "fvg_rt_final_15m.snapshot",
                "frozen_1h_short.snapshot",
                "frozen_4h_short.snapshot",
            ),
        ),
        module(
            "setup_adapter.breakout_return_v1",
            ModuleCategory.SETUP_ADAPTER,
            required_features=("breakout_return.confirmed",),
            produced_features=("setup.confirmed",),
            runtime_component_id="setup_adapter",
            runtime_component_version="0.1.0",
            parameter_schema={"sources": {"type": "array"}},
            fixed_parameters={"sources": ({"profile": "breakout_return_v1"},)},
        ),
        module(
            "setup_adapter.fvg_rt_composite_v1",
            ModuleCategory.SETUP_ADAPTER,
            required_features=(
                "fvg_rt_final_15m.snapshot",
                "frozen_1h_short.snapshot",
                "frozen_4h_short.snapshot",
            ),
            produced_features=("setup.confirmed",),
            runtime_component_id="setup_adapter",
            runtime_component_version="0.1.0",
            parameter_schema={"sources": {"type": "array"}},
            fixed_parameters={"sources": ({"profile": "fvg_rt_composite_v1"},)},
        ),
        module(
            "entry",
            ModuleCategory.ENTRY,
            required_features=("setup.confirmed",),
            produced_features=("entry.plan",),
        ),
        module("protection", ModuleCategory.PROTECTION),
        module("target", ModuleCategory.TARGET),
        module("risk", ModuleCategory.RISK),
    ):
        value.register(item)

    generated = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG, Direction.SHORT}),
        maximum_architectures=100,
    )

    assert len(generated.architectures) == 1
    architecture = generated.architectures[0]
    assert architecture.signal.module_id == "fvg_rt"
    assert tuple(item.module_id for item in architecture.setup_adapters) == (
        "setup_adapter.fvg_rt_composite_v1",
    )
