from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from strategy_factory.adapters import AggregatedCandidate
from strategy_factory.canonical import canonical_sha256
from strategy_factory.contracts import (
    CandidateEvaluation,
    CandidateStrategy,
    FactoryMode,
)
from strategy_factory.errors import CompatibilityError, ContractError
from strategy_factory.promotion import (
    BaselineAwareCandidateSelector,
    BaselineComparisonPolicy,
    PromotionSelection,
    ResultsAggregatorCandidateSelector,
    _baseline_comparison_failures,
    _candidate_from_aggregated,
    _metric,
    _strategy_configuration,
)
from strategy_factory.ranking import (
    Constraint,
    Objective,
    ObjectiveDirection,
    ParetoRanker,
    RankedCandidate,
)

NOW = datetime(2026, 1, 2, tzinfo=UTC)


def _evaluation(
    candidate_id: str,
    *,
    pf: str = "1.20",
    dd: str = "0.10",
    trades: object = 100,
    complexity: str = "1",
    flags: tuple[str, ...] = (),
) -> CandidateEvaluation:
    return CandidateEvaluation(
        evaluation_id=f"evaluation-{candidate_id}",
        candidate_id=candidate_id,
        metrics={
            "profit_factor": Decimal(pf),
            "max_drawdown": Decimal(dd),
            "trade_count": trades,
        },
        constraints={"registry_evidence_clean": True},
        pareto_front=0,
        instability_flags=flags,
        complexity=Decimal(complexity),
        evidence_references=(f"evidence-{candidate_id}",),
    )


def _aggregated(
    candidate_id: str,
    *,
    parameters: dict[str, object] | None = None,
    producer_hash: str | None = None,
    pf: str = "1.20",
    dd: str = "0.10",
    trades: object = 100,
    complexity: str = "1",
    flags: tuple[str, ...] = (),
) -> AggregatedCandidate:
    params = parameters or {"risk": candidate_id}
    return AggregatedCandidate(
        configuration_id=candidate_id,
        configuration_hash=canonical_sha256(params),
        producer_configuration_hash=(producer_hash if producer_hash is not None else "a" * 64),
        canonical_configuration=params,
        run_ids=(f"run-{candidate_id}",),
        evaluation=_evaluation(
            candidate_id,
            pf=pf,
            dd=dd,
            trades=trades,
            complexity=complexity,
            flags=flags,
        ),
    )


def _ranker() -> ParetoRanker:
    return ParetoRanker(
        objectives=(
            Objective("profit_factor", ObjectiveDirection.MAXIMIZE),
            Objective("max_drawdown", ObjectiveDirection.MINIMIZE),
        ),
        constraints=(Constraint("trade_count", minimum=Decimal("1")),),
    )


def _ranked(candidate: CandidateStrategy, reason: str = "PARETO_FRONT_1") -> RankedCandidate:
    return RankedCandidate(
        candidate=candidate,
        pareto_front=1,
        constraint_results={"trade_count": True},
        objective_values={
            "profit_factor": Decimal("1.2"),
            "max_drawdown": Decimal("0.1"),
        },
        selection_reasons=(reason,),
    )


def _candidate(architecture, candidate_id: str = "candidate-a") -> CandidateStrategy:
    params = {"risk": "0.005"}
    return CandidateStrategy(
        candidate_id=candidate_id,
        architecture=architecture,
        parameters=params,
        configuration_hash=canonical_sha256(params),
        lineage_references=("evidence",),
        evaluation=_evaluation(candidate_id),
    )


def test_promotion_selection_contract_edges(architecture) -> None:
    first = _ranked(_candidate(architecture, "candidate-a"))
    second = _ranked(_candidate(architecture, "candidate-b"))

    with pytest.raises(ContractError, match="overlaps"):
        PromotionSelection(
            selected=(first,),
            rejected=(first,),
            producer_configuration_hashes={"candidate-a": "a" * 64},
        )
    with pytest.raises(CompatibilityError, match="no eligible"):
        PromotionSelection(selected=(), rejected=(), producer_configuration_hashes={})
    with pytest.raises(ContractError, match="cover exactly"):
        PromotionSelection(
            selected=(first,), rejected=(), producer_configuration_hashes={"other": "a" * 64}
        )
    with pytest.raises(ContractError, match="baseline control"):
        PromotionSelection(
            selected=(second,),
            rejected=(first,),
            producer_configuration_hashes={"candidate-b": "b" * 64},
            baseline_control=first,
        )
    with pytest.raises(ContractError, match="lowercase SHA-256"):
        PromotionSelection(
            selected=(first,),
            rejected=(),
            producer_configuration_hashes={"candidate-a": "A" * 64},
        )

    selection = PromotionSelection(
        selected=(first, second),
        rejected=(),
        producer_configuration_hashes={
            "candidate-b": "b" * 64,
            "candidate-a": "a" * 64,
        },
    )
    assert selection.activation_hashes == ("a" * 64, "b" * 64)
    assert tuple(selection.producer_configuration_hashes) == ("candidate-a", "candidate-b")


@pytest.mark.parametrize("maximum", [True, 0, -1, Decimal("1")])
def test_results_selector_rejects_invalid_budget(architecture, maximum) -> None:
    selector = ResultsAggregatorCandidateSelector(
        aggregator=SimpleNamespace(candidates_for_experiment=lambda *args, **kwargs: ()),
        ranker=_ranker(),
    )
    with pytest.raises(ContractError, match="positive int"):
        selector.select(
            experiment_id="experiment-a",
            architecture=architecture,
            maximum_candidates=maximum,
            generated_at=NOW,
        )


def test_results_selector_rejects_empty_population_and_missing_producer_hash(architecture) -> None:
    class Aggregator:
        population: tuple[object, ...] = ()

        def candidates_for_experiment(self, experiment_id, *, generated_at):
            assert experiment_id == "experiment-a"
            assert generated_at == NOW
            return self.population

    aggregator = Aggregator()
    selector = ResultsAggregatorCandidateSelector(aggregator=aggregator, ranker=_ranker())
    with pytest.raises(CompatibilityError, match="no candidate population"):
        selector.select(
            experiment_id="experiment-a",
            architecture=architecture,
            maximum_candidates=1,
            generated_at=NOW,
        )

    candidate = _aggregated("candidate-a")
    aggregator.population = (replace(candidate, producer_configuration_hash=None),)
    with pytest.raises(CompatibilityError, match="lacks producer"):
        selector.select(
            experiment_id="experiment-a",
            architecture=architecture,
            maximum_candidates=1,
            generated_at=NOW,
        )


def test_candidate_projection_and_malformed_aggregated_record(architecture) -> None:
    full = {
        "fixed_r.reward_r": "4",
        "fixed_fractional.risk_fraction": "0.005",
        "producer_metadata": "ignored",
    }
    projected = _strategy_configuration(architecture, full)
    assert dict(projected) == {
        "fixed_fractional.risk_fraction": "0.005",
        "fixed_r.reward_r": "4",
    }
    fallback = _strategy_configuration(architecture, {"risk": "0.005"})
    assert dict(fallback) == {"risk": "0.005"}

    with pytest.raises(CompatibilityError, match="no canonical configuration"):
        _candidate_from_aggregated(
            SimpleNamespace(
                configuration_id="candidate-a",
                canonical_configuration=None,
                run_ids=(),
                evaluation=_evaluation("candidate-a"),
            ),
            architecture=architecture,
        )

    converted = _candidate_from_aggregated(
        SimpleNamespace(
            configuration_id="candidate-a",
            canonical_configuration={"risk": "0.005"},
            configuration_hash="c" * 64,
            run_ids=("run-a",),
            evaluation=_evaluation("candidate-a"),
        ),
        architecture=architecture,
    )
    assert converted.lineage_references == (
        "run-a",
        f"results-aggregator-configuration-hash:{'c' * 64}",
    )


@pytest.mark.parametrize(
    ("changes", "message"),
    [
        ({"adapt_minimum_profit_factor_ratio": Decimal("0")}, "positive finite"),
        ({"adapt_maximum_drawdown_ratio": Decimal("NaN")}, "positive finite"),
        ({"minimum_trade_count_ratio": "0.75"}, "positive finite"),
        ({"improvement_epsilon": Decimal("-0.1")}, "non-negative"),
        ({"improvement_epsilon": Decimal("Infinity")}, "non-negative"),
    ],
)
def test_baseline_policy_rejects_invalid_thresholds(changes, message) -> None:
    with pytest.raises(ContractError, match=message):
        BaselineComparisonPolicy(**changes)


def test_baseline_selector_input_validation(architecture) -> None:
    selector = BaselineAwareCandidateSelector(
        aggregator=SimpleNamespace(candidates_for_experiment=lambda *args, **kwargs: ()),
        ranker=_ranker(),
    )
    common = dict(
        experiment_id="experiment-a",
        architecture=architecture,
        maximum_candidates=1,
        generated_at=NOW,
        baseline_configuration_hash="a" * 64,
        mode=FactoryMode.ADAPT,
    )
    with pytest.raises(ContractError, match="adapt or improve"):
        selector.select_with_baseline(**{**common, "mode": FactoryMode.CREATE})
    with pytest.raises(ContractError, match="positive int"):
        selector.select_with_baseline(**{**common, "maximum_candidates": True})
    with pytest.raises(ContractError, match="include_baseline_control"):
        selector.select_with_baseline(**common, include_baseline_control=1)
    with pytest.raises(ContractError, match="complexity_increase"):
        selector.select_with_baseline(**common, maximum_complexity_increase=Decimal("NaN"))
    with pytest.raises(CompatibilityError, match="no candidate population"):
        selector.select_with_baseline(**common)


class _PopulationAggregator:
    def __init__(self, populations: dict[str, tuple[AggregatedCandidate, ...]]) -> None:
        self.populations = populations

    def candidates_for_experiment(self, experiment_id, *, generated_at):
        assert generated_at == NOW
        return self.populations.get(experiment_id, ())


def test_baseline_selector_requires_exactly_one_eligible_baseline(architecture) -> None:
    baseline_params = {"risk": "baseline"}
    baseline_hash = canonical_sha256(baseline_params)
    baseline = _aggregated("baseline", parameters=baseline_params)
    selector = BaselineAwareCandidateSelector(
        aggregator=_PopulationAggregator({"experiment-a": (baseline,)}),
        ranker=_ranker(),
    )
    with pytest.raises(CompatibilityError, match="exactly one"):
        selector.select_with_baseline(
            experiment_id="experiment-a",
            architecture=architecture,
            maximum_candidates=1,
            generated_at=NOW,
            baseline_configuration_hash="f" * 64,
            mode=FactoryMode.ADAPT,
        )

    duplicate = replace(baseline, configuration_id="baseline-copy")
    selector = BaselineAwareCandidateSelector(
        aggregator=_PopulationAggregator({"experiment-a": (baseline, duplicate)}),
        ranker=_ranker(),
    )
    with pytest.raises(CompatibilityError, match="exactly one"):
        selector.select_with_baseline(
            experiment_id="experiment-a",
            architecture=architecture,
            maximum_candidates=1,
            generated_at=NOW,
            baseline_configuration_hash=baseline_hash,
            mode=FactoryMode.ADAPT,
        )

    unstable = replace(baseline, evaluation=_evaluation("baseline", flags=("UNSTABLE",)))
    selector = BaselineAwareCandidateSelector(
        aggregator=_PopulationAggregator({"experiment-a": (unstable,)}),
        ranker=_ranker(),
    )
    with pytest.raises(CompatibilityError, match="failed canonical ranking"):
        selector.select_with_baseline(
            experiment_id="experiment-a",
            architecture=architecture,
            maximum_candidates=1,
            generated_at=NOW,
            baseline_configuration_hash=baseline_hash,
            mode=FactoryMode.ADAPT,
        )


def test_baseline_selector_adapt_rejects_degradation_and_limits_budget(architecture) -> None:
    baseline_params = {"risk": "baseline"}
    baseline = _aggregated("baseline", parameters=baseline_params, pf="1.20", dd="0.10", trades=100)
    passing = _aggregated("passing", pf="1.30", dd="0.09", trades=100)
    overflow = _aggregated("overflow", pf="1.25", dd="0.095", trades=90)
    degraded = _aggregated("degraded", pf="0.80", dd="0.20", trades=20)
    selector = BaselineAwareCandidateSelector(
        aggregator=_PopulationAggregator({"experiment-a": (baseline, passing, overflow, degraded)}),
        ranker=_ranker(),
    )
    result = selector.select_with_baseline(
        experiment_id="experiment-a",
        architecture=architecture,
        maximum_candidates=2,
        generated_at=NOW,
        baseline_configuration_hash=canonical_sha256(baseline_params),
        mode=FactoryMode.ADAPT,
    )
    assert tuple(item.candidate.candidate_id for item in result.selected) == (
        "baseline",
        "passing",
    )
    reasons = {item.candidate.candidate_id: set(item.selection_reasons) for item in result.rejected}
    assert "PROMOTION_BUDGET_LIMIT" in reasons["overflow"]
    assert {
        "BASELINE_TRADE_COUNT_DEGRADATION",
        "BASELINE_PROFIT_FACTOR_DEGRADATION",
        "BASELINE_DRAWDOWN_DEGRADATION",
    } <= reasons["degraded"]
    assert result.baseline_control is not None
    assert "BASELINE_CONTROL_ARM" in result.baseline_control.selection_reasons


def test_baseline_selector_cross_experiment_and_missing_producer_hash(architecture) -> None:
    baseline_params = {"risk": "baseline"}
    baseline = _aggregated("baseline", parameters=baseline_params)
    challenger = _aggregated("challenger", pf="1.30", dd="0.09")
    selector = BaselineAwareCandidateSelector(
        aggregator=_PopulationAggregator(
            {"baseline-experiment": (baseline,), "target-experiment": (challenger,)}
        ),
        ranker=_ranker(),
    )
    common = dict(
        experiment_id="target-experiment",
        architecture=architecture,
        maximum_candidates=1,
        generated_at=NOW,
        baseline_configuration_hash=canonical_sha256(baseline_params),
        mode=FactoryMode.IMPROVE,
        baseline_experiment_id="baseline-experiment",
        baseline_architecture=architecture,
    )
    with pytest.raises(ContractError, match="cross-experiment baseline"):
        selector.select_with_baseline(**common)

    result = selector.select_with_baseline(**common, include_baseline_control=False)
    assert result.selected[0].candidate.candidate_id == "challenger"
    assert result.baseline_control is not None

    missing_hash = replace(challenger, producer_configuration_hash=None)
    selector = BaselineAwareCandidateSelector(
        aggregator=_PopulationAggregator(
            {"baseline-experiment": (baseline,), "target-experiment": (missing_hash,)}
        ),
        ranker=_ranker(),
    )
    with pytest.raises(CompatibilityError, match="lacks producer"):
        selector.select_with_baseline(**common, include_baseline_control=False)


def test_improve_comparison_reasons_and_metric_parsing(architecture) -> None:
    baseline = _candidate(architecture, "baseline")
    assert baseline.evaluation is not None
    challenger = replace(
        _candidate(architecture, "challenger"),
        evaluation=_evaluation("challenger", pf="1.10", dd="0.12", trades=50, complexity="3"),
    )
    failures = _baseline_comparison_failures(
        baseline=baseline,
        challenger=challenger,
        mode=FactoryMode.IMPROVE,
        policy=BaselineComparisonPolicy(),
        maximum_complexity_increase=Decimal("0.5"),
    )
    assert {
        "BASELINE_TRADE_COUNT_DEGRADATION",
        "NO_PROFIT_FACTOR_IMPROVEMENT",
        "NO_DRAWDOWN_IMPROVEMENT",
        "IMPROVEMENT_COMPLEXITY_LIMIT_EXCEEDED",
        "NO_MEASURABLE_BASELINE_IMPROVEMENT",
    } <= set(failures)

    no_evaluation = replace(challenger, evaluation=None)
    assert _baseline_comparison_failures(
        baseline=baseline,
        challenger=no_evaluation,
        mode=FactoryMode.IMPROVE,
        policy=BaselineComparisonPolicy(),
        maximum_complexity_increase=Decimal("0"),
    ) == ("BASELINE_COMPARISON_MISSING_EVALUATION",)

    missing_metric = replace(
        challenger,
        evaluation=replace(
            challenger.evaluation,
            metrics={"profit_factor": Decimal("1.2")},
        ),
    )
    assert _baseline_comparison_failures(
        baseline=baseline,
        challenger=missing_metric,
        mode=FactoryMode.IMPROVE,
        policy=BaselineComparisonPolicy(),
        maximum_complexity_increase=Decimal("0"),
    ) == ("BASELINE_COMPARISON_METRIC_MISSING",)

    assert _metric({}, "missing") is None
    assert _metric({"value": True}, "value") is None
    assert _metric({"value": "not-a-number"}, "value") is None
    assert _metric({"value": Decimal("Infinity")}, "value") is None
    assert _metric({"value": "1.25"}, "value") == Decimal("1.25")
