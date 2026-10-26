from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from strategy_factory.canonical import canonical_json_bytes, strict_json_loads
from strategy_factory.contract_codec import (
    decode_experiment_plan,
    decode_instrument_profile,
    decode_market_regime_definition,
    decode_module_reference,
    decode_parameter_space,
    decode_strategy_architecture,
    decode_strategy_hypothesis,
)
from strategy_factory.contracts import (
    ExperimentPlan,
    FactoryStage,
    InstrumentProfile,
    MarketRegimeDefinition,
    ParameterSpace,
    StrategyHypothesis,
)
from strategy_factory.errors import ContractError


def primitive(value):
    return strict_json_loads(canonical_json_bytes(value))


def profile(descriptor) -> InstrumentProfile:
    return InstrumentProfile(
        profile_id="profile-codec",
        dataset_fingerprint=descriptor.fingerprint,
        symbol=descriptor.symbol,
        market_type=descriptor.market_type,
        generated_at=datetime(2026, 1, 2, tzinfo=UTC),
        history_days=Decimal("30.5"),
        observations=100,
        liquidity_metrics={"volume_mean": Decimal("12.5")},
        volatility_metrics={"return_std": Decimal("0.01")},
        return_distribution={"skew": Decimal("0.2")},
        trend_metrics={"directional_imbalance": Decimal("0.1")},
        mean_reversion_metrics={"lag1_autocorrelation": Decimal("-0.05")},
        seasonality_metrics={"hourly": [1, 2]},
        tail_risk_metrics={"minimum_return": Decimal("-0.1")},
        funding_metrics={"available": False},
        cost_sensitivity={"turnover_proxy": Decimal("0.02")},
        stability_metrics={"method": "fixture"},
        warnings=("SHORT_HISTORY",),
    )


def test_profile_and_regime_round_trip(descriptor) -> None:
    original = profile(descriptor)
    assert decode_instrument_profile(primitive(original)) == original

    regime = MarketRegimeDefinition(
        regime_id="uptrend",
        name="Uptrend",
        description="Lagged point-in-time trend definition.",
        feature_version="1.0",
        point_in_time=True,
        required_timeframes=("5m", "15m"),
        parameters={"ema_period": 200, "threshold": Decimal("0.1")},
    )
    assert decode_market_regime_definition(primitive(regime)) == regime


def test_module_hypothesis_architecture_and_space_round_trip(architecture) -> None:
    for module in architecture.modules:
        assert decode_module_reference(primitive(module)) == module

    hypothesis = StrategyHypothesis(
        hypothesis_id="hyp-codec",
        description="Test a registered signal family.",
        expected_edge="Retest continuation.",
        applicability=("uptrend",),
        risks=("cost sensitivity",),
        module_candidates=(architecture.signal,),
        parameter_ranges={architecture.signal.key: {"lookback": (10, 20)}},
        falsification_criteria={"minimum_trade_count": 30},
        estimated_experiments=10,
        novelty=Decimal("0.5"),
        origin={"generator": "fixture"},
    )
    assert decode_strategy_hypothesis(primitive(hypothesis)) == hypothesis
    assert decode_strategy_architecture(primitive(architecture)) == architecture

    space = ParameterSpace(
        space_id="space-codec",
        architecture_id=architecture.architecture_id,
        dimensions={"target.reward_r": (Decimal("2"), Decimal("3"))},
        constraints=({"left": "target.reward_r", "operator": ">=", "right": 2},),
        maximum_combinations=2,
    )
    assert decode_parameter_space(primitive(space)) == space


def test_experiment_plan_round_trip(factory_request, architecture) -> None:
    space = ParameterSpace(
        space_id="space-plan-codec",
        architecture_id=architecture.architecture_id,
        dimensions={"target.reward_r": (2, 3)},
        maximum_combinations=2,
    )
    plan = ExperimentPlan(
        plan_id="sf-plan-codec",
        factory_run_id="sf-run-codec",
        stage=FactoryStage.STRUCTURAL_SCREENING,
        architecture=architecture,
        parameter_space=space,
        dataset_fingerprint=factory_request.dataset.fingerprint,
        research_specification={"search_method": "grid"},
        resource_limits={"maximum_runs": 2},
        deterministic_seed=42,
        manifest_id="manifest-codec",
    )
    assert decode_experiment_plan(primitive(plan)) == plan


def test_codec_rejects_unknown_fields_and_malformed_decimal(descriptor) -> None:
    value = primitive(profile(descriptor))
    value["unknown"] = True
    with pytest.raises(ContractError, match="fields are invalid"):
        decode_instrument_profile(value)

    value = primitive(profile(descriptor))
    value["history_days"] = {"$decimal": "NaN"}
    with pytest.raises(ContractError, match="finite"):
        decode_instrument_profile(value)
