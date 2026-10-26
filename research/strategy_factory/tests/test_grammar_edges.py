from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from conftest import module
from test_grammar import catalog

from strategy_factory.contracts import (
    Direction,
    MarketType,
    ModuleCategory,
    ModuleMaturity,
)
from strategy_factory.errors import CompatibilityError, ContractError
from strategy_factory.grammar import GrammarPolicy, StrategyGrammar, _subsets


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"maximum_context_filters": True}, "non-negative"),
        ({"maximum_setup_adapters": -1}, "non-negative"),
        ({"maximum_risk_adapters": -1}, "non-negative"),
        ({"maximum_position_management": -1}, "non-negative"),
        ({"maximum_portfolio_constraints": -1}, "non-negative"),
        ({"minimum_final_coverage": -1}, r"\[0, 100\]"),
        ({"minimum_final_coverage": 101}, r"\[0, 100\]"),
    ],
)
def test_grammar_policy_rejects_invalid(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        GrammarPolicy(**changes)


def test_generate_rejects_empty_directions_and_nonpositive_limit(descriptor) -> None:
    grammar = StrategyGrammar(catalog())
    with pytest.raises(ContractError, match="directions"):
        grammar.generate(dataset=descriptor, directions=frozenset())
    with pytest.raises(ContractError, match="maximum_architectures"):
        grammar.generate(
            dataset=descriptor,
            directions=frozenset({Direction.LONG}),
            maximum_architectures=0,
        )


def test_generate_truncates_and_records_fixed_parameters(descriptor) -> None:
    value = catalog()
    value.register(
        module(
            "second-signal",
            ModuleCategory.SIGNAL,
            produced_features=("setup",),
            parameter_schema={"period": {"type": "integer"}},
            parameter_search_hints={"period": {"values": [1, 2]}},
        )
    )
    result = StrategyGrammar(value).generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        maximum_architectures=1,
    )
    assert len(result.architectures) == 1
    assert result.truncated


def test_validate_rejects_market_mismatch(descriptor, architecture) -> None:
    other = replace(architecture, market_type=MarketType.SPOT)
    with pytest.raises(CompatibilityError, match="MARKET_MISMATCH"):
        StrategyGrammar(catalog()).validate(other, dataset=descriptor)


def test_eligible_filter_paths(descriptor) -> None:
    value = catalog()
    # Corrupt only the eligibility metadata to exercise fail-closed filtering.
    wrong_market = module("wrong-market", ModuleCategory.SIGNAL, produced_features=("setup",))
    object.__setattr__(wrong_market, "supported_markets", frozenset({MarketType.SPOT}))
    direction = copy.copy(wrong_market)
    object.__setattr__(direction, "module_id", "wrong-direction")
    object.__setattr__(direction, "supported_markets", frozenset({descriptor.market_type}))
    object.__setattr__(direction, "supported_directions", frozenset({Direction.SHORT}))
    timeframe = copy.copy(direction)
    object.__setattr__(timeframe, "module_id", "wrong-timeframe")
    object.__setattr__(timeframe, "supported_directions", frozenset({Direction.LONG}))
    object.__setattr__(timeframe, "required_timeframes", ("1d",))
    required_data = copy.copy(timeframe)
    object.__setattr__(required_data, "module_id", "missing-data")
    object.__setattr__(required_data, "required_timeframes", ())
    object.__setattr__(required_data, "required_data", frozenset({"funding"}))
    experimental = copy.copy(required_data)
    object.__setattr__(experimental, "module_id", "experimental")
    object.__setattr__(experimental, "required_data", frozenset())
    object.__setattr__(experimental, "maturity", ModuleMaturity.EXPERIMENTAL)
    for item in (wrong_market, direction, timeframe, required_data, experimental):
        value.register(item)  # deliberate malformed metadata fixture

    grammar = StrategyGrammar(value)
    generated = grammar.generate(
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        forbidden_module_ids=frozenset({"wrong-market"}),
        allowed_signal_module_ids=frozenset({"fvg"}),
        final_candidate=True,
    )
    assert generated.architectures
    assert {item.signal.module_id for item in generated.architectures} == {"fvg"}


def _reasons(
    grammar, descriptor, modules, *, final=False, external=frozenset(), data=frozenset({"ohlcv"})
):
    return grammar._combination_rejections(
        modules=modules,
        dataset=descriptor,
        directions=frozenset({Direction.LONG}),
        final_candidate=final,
        external_features=external,
        available_data=data,
    )


def test_combination_rejection_market_direction_timeframe_data_and_final(descriptor) -> None:
    grammar = StrategyGrammar(catalog(), GrammarPolicy(minimum_final_coverage=90))
    base = module("x", ModuleCategory.SIGNAL, produced_features=("x",))
    bad = copy.copy(base)
    object.__setattr__(bad, "supported_markets", frozenset({MarketType.SPOT}))
    object.__setattr__(bad, "supported_directions", frozenset({Direction.SHORT}))
    object.__setattr__(bad, "required_timeframes", ("1d",))
    object.__setattr__(bad, "required_data", frozenset({"funding", "instrument_rules"}))
    object.__setattr__(bad, "maturity", ModuleMaturity.EXPERIMENTAL)
    reasons = _reasons(grammar, descriptor, (bad,), final=True)
    assert {
        "UNSUPPORTED_MARKET",
        "DIRECTION_MISMATCH",
        "TIMEFRAME_UNAVAILABLE",
        "REQUIRED_DATA_MISSING",
        "FUNDING_DATA_MISSING",
        "MODULE_NOT_FINAL_ELIGIBLE",
        "STOP_REQUIRED",
    } <= reasons


def test_combination_rejection_tags_duplicates_versions_and_self_dependency(descriptor) -> None:
    grammar = StrategyGrammar(catalog())
    first = module(
        "same",
        ModuleCategory.SIGNAL,
        required_features=("self",),
        produced_features=("self",),
        conflict_tags=frozenset({"tag"}),
    )
    second = copy.copy(first)
    object.__setattr__(second, "compatibility_tags", frozenset({"tag"}))
    object.__setattr__(second, "runtime_component_version", "2.0.0")
    reasons = _reasons(grammar, descriptor, (first, second))
    assert {
        "CONFLICTING_MODULE_TAGS",
        "DUPLICATE_LOGICAL_MODULE_ROLE",
        "RUNTIME_COMPONENT_VERSION_CONFLICT",
        "SELF_FEATURE_DEPENDENCY",
        "STOP_REQUIRED",
    } <= reasons


def test_combination_rejection_multirole_contracts_and_facets(descriptor) -> None:
    grammar = StrategyGrammar(catalog())
    roles = frozenset({ModuleCategory.PROTECTION, ModuleCategory.TARGET})
    protection = module(
        "shared-protection",
        ModuleCategory.PROTECTION,
        runtime_component_id="shared",
        runtime_component_version="1.0.0",
        runtime_role_categories=roles,
        role_facet="protection",
    )
    target = module(
        "shared-target",
        ModuleCategory.TARGET,
        runtime_component_id="shared",
        runtime_component_version="1.0.0",
        runtime_role_categories=roles,
        role_facet="target",
    )
    undeclared = copy.copy(target)
    object.__setattr__(undeclared, "runtime_role_categories", frozenset({ModuleCategory.TARGET}))
    assert "RUNTIME_COMPONENT_MULTIROLE_UNDECLARED" in _reasons(
        grammar, descriptor, (protection, undeclared)
    )
    duplicate_facet = copy.copy(target)
    object.__setattr__(duplicate_facet, "role_facet", "protection")
    assert "RUNTIME_COMPONENT_ROLE_FACET_CONFLICT" in _reasons(
        grammar, descriptor, (protection, duplicate_facet)
    )
    conflicting = copy.copy(target)
    object.__setattr__(conflicting, "import_path", "other:Target")
    assert "RUNTIME_COMPONENT_CONTRACT_CONFLICT" in _reasons(
        grammar, descriptor, (protection, conflicting)
    )


def test_available_data_and_final_eligibility(descriptor) -> None:
    grammar = StrategyGrammar(catalog(), GrammarPolicy(require_deterministic=False))
    enriched = replace(
        descriptor,
        funding_uri="funding.csv",
        funding_sha256="f" * 64,
        exchange_info_sha256="e" * 64,
    )
    assert grammar._available_data(enriched, frozenset({"portfolio_snapshot"})) == frozenset(
        {"ohlcv", "funding", "instrument_rules", "portfolio_snapshot"}
    )
    nondeterministic = copy.copy(module("x", ModuleCategory.SIGNAL))
    object.__setattr__(nondeterministic, "deterministic", False)
    assert grammar._is_final_eligible(nondeterministic)


def test_subsets_respects_limit() -> None:
    values = (
        module("a", ModuleCategory.CONTEXT_FILTER),
        module("b", ModuleCategory.CONTEXT_FILTER),
    )
    assert _subsets(values, 0) == ((),)
    assert len(_subsets(values, 1)) == 3
