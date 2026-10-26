from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from conftest import module

from strategy_factory.contracts import ModuleCategory
from strategy_factory.dataset import DatasetAnalyzer, DatasetValidationPolicy
from strategy_factory.hypothesis import (
    HypothesisGenerator,
    RegimeLibrary,
    _family_evidence,
    _module_matches_family,
    _number,
)
from strategy_factory.registry import StrategyModuleCatalog


def _profile(factory_request):
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
    return profile


def test_regime_library_deduplicates_single_timeframe_requirements() -> None:
    regimes = RegimeLibrary.default(("5m",))
    assert all(item.required_timeframes == ("5m",) for item in regimes)


def test_module_family_matching_supports_aliases_tags_and_rejection() -> None:
    fvg = module("three-candle-fvg", ModuleCategory.SIGNAL)
    tagged = module(
        "custom_signal",
        ModuleCategory.SIGNAL,
        compatibility_tags=frozenset({"range-breakout"}),
    )
    assert _module_matches_family(fvg, "trend-continuation")
    assert _module_matches_family(tagged, "volatility_expansion")
    assert not _module_matches_family(tagged, "mean_reversion")


def test_family_evidence_exercises_all_eligibility_paths(factory_request) -> None:
    profile = _profile(factory_request)

    mean_reversion = _family_evidence(
        "mean-reversion",
        replace(
            profile,
            mean_reversion_metrics={"lag1_autocorrelation": "0.20"},
        ),
    )
    assert mean_reversion["eligible"] is False

    breakout = _family_evidence(
        "range_breakout",
        replace(profile, volatility_metrics={"return_std": "0"}),
    )
    assert breakout["eligible"] is False

    trend = _family_evidence(
        "momentum",
        replace(profile, trend_metrics={"directional_imbalance": "1.1"}),
    )
    assert trend["eligible"] is False

    generic = _family_evidence("custom_family", profile)
    assert generic == {
        "eligible": True,
        "expected_edge": "registered family-specific behavior requiring empirical falsification",
        "applicability": ("profile-dependent",),
        "risks": ("insufficient evidence", "multiple testing"),
    }


def test_number_conversion_is_fail_closed() -> None:
    assert _number(True) is None
    assert _number(None) is None
    assert _number(object()) is None
    assert _number("not-a-number") is None
    assert _number("1.25") == 1.25


def test_generator_skips_ineligible_family(factory_request) -> None:
    profile = replace(
        _profile(factory_request),
        mean_reversion_metrics={"lag1_autocorrelation": "0.20"},
    )
    catalog = StrategyModuleCatalog()
    catalog.register(
        module(
            "mean_reversion_signal",
            ModuleCategory.SIGNAL,
            compatibility_tags=frozenset({"mean_reversion"}),
        )
    )
    request = replace(
        factory_request,
        allowed_strategy_families=frozenset({"mean_reversion"}),
    )
    assert HypothesisGenerator(catalog).generate(request=request, profile=profile) == ()


def test_generator_skips_family_without_registered_signal(factory_request) -> None:
    request = replace(
        factory_request,
        allowed_strategy_families=frozenset({"unknown_family"}),
    )
    assert (
        HypothesisGenerator(StrategyModuleCatalog()).generate(
            request=request,
            profile=_profile(factory_request),
        )
        == ()
    )


def test_generator_marks_baseline_seeded_hypothesis_as_lower_novelty(
    factory_request,
) -> None:
    catalog = StrategyModuleCatalog()
    catalog.register(module("fvg", ModuleCategory.SIGNAL))
    request = replace(factory_request, baseline_strategy_id="baseline-v1")
    generated = HypothesisGenerator(catalog).generate(
        request=request,
        profile=_profile(factory_request),
    )
    assert generated
    assert all(item.novelty == Decimal("0.25") for item in generated)
    assert all(item.origin["baseline_strategy_id"] == "baseline-v1" for item in generated)
