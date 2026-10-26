"""Explainable candidate selection from the canonical Results Aggregator."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType

from .adapters import AggregatedCandidate, ResultsAggregatorAdapter
from .canonical import canonical_sha256
from .contracts import CandidateStrategy, FactoryMode, StrategyArchitecture
from .errors import CompatibilityError, ContractError
from .ranking import ParetoRanker, RankedCandidate


@dataclass(frozen=True, slots=True)
class PromotionSelection:
    """Selected and rejected candidates with their explicit Pareto evidence."""

    selected: tuple[RankedCandidate, ...]
    rejected: tuple[RankedCandidate, ...]
    producer_configuration_hashes: Mapping[str, str]
    baseline_control: RankedCandidate | None = None
    allow_empty: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.allow_empty, bool):
            raise ContractError("allow_empty must be boolean")
        selected_ids = {item.candidate.candidate_id for item in self.selected}
        rejected_ids = {item.candidate.candidate_id for item in self.rejected}
        if selected_ids & rejected_ids:
            raise ContractError("promotion selection overlaps selected and rejected candidates")
        if not self.selected and not self.allow_empty:
            raise CompatibilityError("promotion selection produced no eligible candidates")
        if set(self.producer_configuration_hashes) != selected_ids:
            raise ContractError(
                "producer configuration hashes must cover exactly the selected candidates"
            )
        if (
            self.baseline_control is not None
            and self.baseline_control.candidate.candidate_id in rejected_ids
        ):
            raise ContractError("baseline control cannot also be rejected")
        for value in self.producer_configuration_hashes.values():
            if (
                not isinstance(value, str)
                or len(value) != 64
                or any(character not in "0123456789abcdef" for character in value)
            ):
                raise ContractError("producer configuration hash must be lowercase SHA-256")
        object.__setattr__(
            self,
            "producer_configuration_hashes",
            MappingProxyType(dict(sorted(self.producer_configuration_hashes.items()))),
        )

    @property
    def activation_hashes(self) -> tuple[str, ...]:
        return tuple(
            self.producer_configuration_hashes[item.candidate.candidate_id]
            for item in self.selected
        )


@dataclass(frozen=True, slots=True)
class CrossExperimentSelection:
    """One globally ranked finalist set across multiple terminal experiments."""

    selected: tuple[RankedCandidate, ...]
    rejected: tuple[RankedCandidate, ...]
    source_experiment_ids: Mapping[str, str]
    producer_configuration_hashes: Mapping[str, str]

    def __post_init__(self) -> None:
        selected_ids = {item.candidate.candidate_id for item in self.selected}
        rejected_ids = {item.candidate.candidate_id for item in self.rejected}
        if selected_ids & rejected_ids:
            raise ContractError("cross-experiment selection overlaps selected and rejected")
        if set(self.source_experiment_ids) != selected_ids:
            raise ContractError("source experiment identities must cover selected finalists")
        if set(self.producer_configuration_hashes) != selected_ids:
            raise ContractError("producer hashes must cover selected finalists")
        object.__setattr__(
            self,
            "source_experiment_ids",
            MappingProxyType(dict(sorted(self.source_experiment_ids.items()))),
        )
        object.__setattr__(
            self,
            "producer_configuration_hashes",
            MappingProxyType(dict(sorted(self.producer_configuration_hashes.items()))),
        )


class ResultsAggregatorCandidateSelector:
    """Build CandidateStrategy records and apply an explainable Pareto policy."""

    def __init__(
        self,
        *,
        aggregator: ResultsAggregatorAdapter,
        ranker: ParetoRanker,
    ) -> None:
        self._aggregator = aggregator
        self._ranker = ranker

    def select(
        self,
        *,
        experiment_id: str,
        architecture: StrategyArchitecture,
        maximum_candidates: int,
        generated_at: datetime,
        allow_empty: bool = False,
    ) -> PromotionSelection:
        if (
            isinstance(maximum_candidates, bool)
            or not isinstance(maximum_candidates, int)
            or maximum_candidates < 1
        ):
            raise ContractError("maximum_candidates must be a positive int")
        if not isinstance(allow_empty, bool):
            raise ContractError("allow_empty must be boolean")
        aggregated = self._aggregator.candidates_for_experiment(
            experiment_id,
            generated_at=generated_at,
        )
        if not aggregated:
            raise CompatibilityError(
                "Results Aggregator has no candidate population for the experiment"
            )
        candidates = tuple(
            _candidate_from_aggregated(item, architecture=architecture) for item in aggregated
        )
        ranked = self._ranker.rank(candidates)
        selected = ranked.ranked[:maximum_candidates]
        overflow = tuple(
            RankedCandidate(
                candidate=item.candidate,
                pareto_front=item.pareto_front,
                constraint_results=item.constraint_results,
                objective_values=item.objective_values,
                selection_reasons=(*item.selection_reasons, "PROMOTION_BUDGET_LIMIT"),
            )
            for item in ranked.ranked[maximum_candidates:]
        )
        by_id = {item.configuration_id: item for item in aggregated}
        producer_hashes: dict[str, str] = {}
        for item in selected:
            candidate = by_id[item.candidate.candidate_id]
            producer_hash = candidate.producer_configuration_hash
            if producer_hash is None:
                raise CompatibilityError(
                    "Results Aggregator candidate lacks producer configuration hash"
                )
            producer_hashes[item.candidate.candidate_id] = producer_hash
        return PromotionSelection(
            selected=selected,
            rejected=(*ranked.rejected, *overflow),
            producer_configuration_hashes=producer_hashes,
            allow_empty=allow_empty,
        )

    def select_across_experiments(
        self,
        *,
        experiments: tuple[tuple[str, StrategyArchitecture], ...],
        allowed_producer_hashes: Mapping[str, frozenset[str]],
        maximum_candidates: int,
        generated_at: datetime,
    ) -> CrossExperimentSelection:
        """Rank all terminal experiment populations together before freezing finalists."""

        if (
            isinstance(maximum_candidates, bool)
            or not isinstance(maximum_candidates, int)
            or maximum_candidates < 1
        ):
            raise ContractError("maximum_candidates must be a positive int")
        if not experiments:
            raise CompatibilityError("cross-experiment selection requires experiments")
        experiment_ids = {item[0] for item in experiments}
        if set(allowed_producer_hashes) != experiment_ids:
            raise ContractError(
                "allowed producer hashes must cover exactly the finalist source experiments"
            )
        for hashes in allowed_producer_hashes.values():
            if not isinstance(hashes, frozenset) or any(
                not isinstance(item, str)
                or len(item) != 64
                or any(character not in "0123456789abcdef" for character in item)
                for item in hashes
            ):
                raise ContractError("allowed producer hashes must be lowercase SHA-256 values")

        candidates: list[CandidateStrategy] = []
        source_by_candidate: dict[str, str] = {}
        producer_by_candidate: dict[str, str] = {}
        for experiment_id, architecture in sorted(
            experiments,
            key=lambda item: (item[0], item[1].architecture_id),
        ):
            allowed_hashes = allowed_producer_hashes[experiment_id]
            if not allowed_hashes:
                continue
            aggregated = self._aggregator.candidates_for_experiment(
                experiment_id,
                generated_at=generated_at,
            )
            if not aggregated:
                raise CompatibilityError(
                    "Results Aggregator has no candidate population for an experiment"
                )
            refined_population = tuple(
                item for item in aggregated if item.producer_configuration_hash in allowed_hashes
            )
            if len(refined_population) != len(allowed_hashes):
                raise CompatibilityError(
                    "terminal Results Aggregator population does not cover every "
                    "LOCAL_REFINEMENT configuration"
                )
            for item in refined_population:
                candidate = _candidate_from_aggregated(item, architecture=architecture)
                candidate_id = candidate.candidate_id
                if candidate_id in source_by_candidate:
                    raise CompatibilityError(
                        "candidate identity is duplicated across terminal experiments"
                    )
                producer_hash = item.producer_configuration_hash
                if producer_hash is None:
                    raise CompatibilityError(
                        "Results Aggregator candidate lacks producer configuration hash"
                    )
                candidates.append(candidate)
                source_by_candidate[candidate_id] = experiment_id
                producer_by_candidate[candidate_id] = producer_hash

        ranked = self._ranker.rank(tuple(candidates))
        selected = ranked.ranked[:maximum_candidates]
        overflow = tuple(
            RankedCandidate(
                candidate=item.candidate,
                pareto_front=item.pareto_front,
                constraint_results=item.constraint_results,
                objective_values=item.objective_values,
                selection_reasons=(
                    *item.selection_reasons,
                    "GLOBAL_FINALIST_BUDGET_LIMIT",
                ),
            )
            for item in ranked.ranked[maximum_candidates:]
        )
        selected_ids = {item.candidate.candidate_id for item in selected}
        return CrossExperimentSelection(
            selected=selected,
            rejected=(*ranked.rejected, *overflow),
            source_experiment_ids={
                candidate_id: source_by_candidate[candidate_id] for candidate_id in selected_ids
            },
            producer_configuration_hashes={
                candidate_id: producer_by_candidate[candidate_id] for candidate_id in selected_ids
            },
        )


@dataclass(frozen=True, slots=True)
class BaselineComparisonPolicy:
    """Transparent transfer/improvement thresholds against an unchanged control arm."""

    adapt_minimum_profit_factor_ratio: Decimal = Decimal("0.90")
    adapt_maximum_drawdown_ratio: Decimal = Decimal("1.25")
    minimum_trade_count_ratio: Decimal = Decimal("0.75")
    improvement_epsilon: Decimal = Decimal("0.0001")

    def __post_init__(self) -> None:
        for name in (
            "adapt_minimum_profit_factor_ratio",
            "adapt_maximum_drawdown_ratio",
            "minimum_trade_count_ratio",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
                raise ContractError(f"{name} must be a positive finite Decimal")
        if (
            not isinstance(self.improvement_epsilon, Decimal)
            or not self.improvement_epsilon.is_finite()
            or self.improvement_epsilon < 0
        ):
            raise ContractError("improvement_epsilon must be a non-negative Decimal")


class BaselineAwareCandidateSelector:
    """Compare target candidates with an immutable control arm, including cross-experiment."""

    def __init__(
        self,
        *,
        aggregator: ResultsAggregatorAdapter,
        ranker: ParetoRanker,
        policy: BaselineComparisonPolicy | None = None,
    ) -> None:
        self._aggregator = aggregator
        self._ranker = ranker
        self._policy = policy or BaselineComparisonPolicy()

    def select_with_baseline(
        self,
        *,
        experiment_id: str,
        architecture: StrategyArchitecture,
        maximum_candidates: int,
        generated_at: datetime,
        baseline_configuration_hash: str,
        mode: FactoryMode,
        maximum_complexity_increase: Decimal = Decimal("0"),
        baseline_experiment_id: str | None = None,
        baseline_architecture: StrategyArchitecture | None = None,
        include_baseline_control: bool = True,
    ) -> PromotionSelection:
        if mode not in {FactoryMode.ADAPT, FactoryMode.IMPROVE}:
            raise ContractError("baseline-aware selection supports adapt or improve mode")
        if (
            isinstance(maximum_candidates, bool)
            or not isinstance(maximum_candidates, int)
            or maximum_candidates < 1
        ):
            raise ContractError("maximum_candidates must be a positive int")
        if not isinstance(include_baseline_control, bool):
            raise ContractError("include_baseline_control must be bool")
        if (
            not isinstance(maximum_complexity_increase, Decimal)
            or not maximum_complexity_increase.is_finite()
            or maximum_complexity_increase < 0
        ):
            raise ContractError("maximum_complexity_increase must be a non-negative Decimal")
        baseline_experiment_id = baseline_experiment_id or experiment_id
        baseline_architecture = baseline_architecture or architecture
        target_population = self._population(
            experiment_id, architecture=architecture, generated_at=generated_at
        )
        if baseline_experiment_id == experiment_id:
            baseline_population = target_population
        else:
            baseline_population = self._population(
                baseline_experiment_id,
                architecture=baseline_architecture,
                generated_at=generated_at,
            )
        baseline_matches = tuple(
            item
            for item in baseline_population
            if canonical_sha256(
                _strategy_configuration(baseline_architecture, item.canonical_configuration)
            )
            == baseline_configuration_hash
        )
        if len(baseline_matches) != 1:
            raise CompatibilityError(
                "Results Aggregator must contain exactly one unchanged baseline configuration"
            )
        baseline_item = baseline_matches[0]
        baseline_candidates = self._candidate_records(
            baseline_population, architecture=baseline_architecture
        )
        baseline_ranking = self._ranker.rank(baseline_candidates)
        baseline_rejected_ids = {item.candidate.candidate_id for item in baseline_ranking.rejected}
        baseline_ranked = {
            item.candidate.candidate_id: item
            for item in (*baseline_ranking.ranked, *baseline_ranking.rejected)
        }[baseline_item.configuration_id]
        if baseline_item.configuration_id in baseline_rejected_ids:
            raise CompatibilityError(
                "unchanged baseline control failed canonical ranking constraints"
            )
        if baseline_ranked.candidate.evaluation is None:
            raise CompatibilityError("baseline control has no evaluation")
        baseline_control = RankedCandidate(
            candidate=baseline_ranked.candidate,
            pareto_front=baseline_ranked.pareto_front,
            constraint_results=baseline_ranked.constraint_results,
            objective_values=baseline_ranked.objective_values,
            selection_reasons=(
                *baseline_ranked.selection_reasons,
                "BASELINE_CONTROL_ARM",
            ),
        )

        target_candidates = self._candidate_records(target_population, architecture=architecture)
        target_ranking = self._ranker.rank(target_candidates)
        selected: list[RankedCandidate] = []
        rejected: list[RankedCandidate] = list(target_ranking.rejected)
        if include_baseline_control:
            if baseline_experiment_id != experiment_id:
                raise ContractError(
                    "cross-experiment baseline cannot be activated in target experiment"
                )
            selected.append(baseline_control)
        challengers: list[RankedCandidate] = []
        for item in target_ranking.ranked:
            if (
                baseline_experiment_id == experiment_id
                and item.candidate.candidate_id == baseline_item.configuration_id
            ):
                continue
            reasons = _baseline_comparison_failures(
                baseline=baseline_control.candidate,
                challenger=item.candidate,
                mode=mode,
                policy=self._policy,
                maximum_complexity_increase=maximum_complexity_increase,
            )
            if reasons:
                rejected.append(
                    RankedCandidate(
                        candidate=item.candidate,
                        pareto_front=item.pareto_front,
                        constraint_results=item.constraint_results,
                        objective_values=item.objective_values,
                        selection_reasons=(*item.selection_reasons, *reasons),
                    )
                )
            else:
                challengers.append(
                    RankedCandidate(
                        candidate=item.candidate,
                        pareto_front=item.pareto_front,
                        constraint_results=item.constraint_results,
                        objective_values=item.objective_values,
                        selection_reasons=(
                            *item.selection_reasons,
                            "BASELINE_COMPARISON_PASSED",
                        ),
                    )
                )
        available_slots = maximum_candidates - len(selected)
        selected.extend(challengers[:available_slots])
        rejected.extend(
            RankedCandidate(
                candidate=item.candidate,
                pareto_front=item.pareto_front,
                constraint_results=item.constraint_results,
                objective_values=item.objective_values,
                selection_reasons=(*item.selection_reasons, "PROMOTION_BUDGET_LIMIT"),
            )
            for item in challengers[available_slots:]
        )
        if not selected:
            raise CompatibilityError("no challenger passed comparison with the unchanged baseline")
        target_by_id = {item.configuration_id: item for item in target_population}
        producer_hashes: dict[str, str] = {}
        for item in selected:
            source = (
                baseline_item
                if item.candidate.candidate_id == baseline_item.configuration_id
                else target_by_id[item.candidate.candidate_id]
            )
            producer_hash = source.producer_configuration_hash
            if producer_hash is None:
                raise CompatibilityError(
                    "Results Aggregator candidate lacks producer configuration hash"
                )
            producer_hashes[item.candidate.candidate_id] = producer_hash
        rejected.sort(key=lambda item: item.candidate.candidate_id)
        return PromotionSelection(
            selected=tuple(selected),
            rejected=tuple(rejected),
            producer_configuration_hashes=producer_hashes,
            baseline_control=baseline_control,
        )

    def _population(
        self,
        experiment_id: str,
        *,
        architecture: StrategyArchitecture,
        generated_at: datetime,
    ) -> tuple[AggregatedCandidate, ...]:
        aggregated = self._aggregator.candidates_for_experiment(
            experiment_id, generated_at=generated_at
        )
        if not aggregated:
            raise CompatibilityError(
                "Results Aggregator has no candidate population for the experiment"
            )
        return aggregated

    @staticmethod
    def _candidate_records(
        population: tuple[AggregatedCandidate, ...],
        *,
        architecture: StrategyArchitecture,
    ) -> tuple[CandidateStrategy, ...]:
        return tuple(
            _candidate_from_aggregated(item, architecture=architecture) for item in population
        )


def _strategy_configuration(
    architecture: StrategyArchitecture,
    canonical_configuration: Mapping[str, object],
) -> Mapping[str, object]:
    """Project producer metadata out of a registry configuration."""

    expected_names = {
        f"{module.runtime_component_id}.{parameter_name}"
        for module in architecture.runtime_modules
        for parameter_name in module.parameter_schema
    }
    projected = {
        name: canonical_configuration[name]
        for name in sorted(expected_names)
        if name in canonical_configuration
    }
    if projected:
        return MappingProxyType(projected)
    return MappingProxyType(dict(canonical_configuration))


def _candidate_from_aggregated(
    item: object,
    *,
    architecture: StrategyArchitecture,
) -> CandidateStrategy:
    canonical_configuration = getattr(item, "canonical_configuration", None)
    if not isinstance(canonical_configuration, Mapping):
        raise CompatibilityError("Results Aggregator candidate has no canonical configuration")
    parameters = _strategy_configuration(architecture, canonical_configuration)
    registry_hash = getattr(item, "configuration_hash", None)
    lineage = tuple(getattr(item, "run_ids", ()))
    if isinstance(registry_hash, str):
        lineage = (
            *lineage,
            f"results-aggregator-configuration-hash:{registry_hash}",
        )
    return CandidateStrategy(
        candidate_id=str(getattr(item, "configuration_id", "")),
        architecture=architecture,
        parameters=parameters,
        configuration_hash=canonical_sha256(parameters),
        lineage_references=lineage,
        evaluation=getattr(item, "evaluation", None),
    )


def _baseline_comparison_failures(
    *,
    baseline: CandidateStrategy,
    challenger: CandidateStrategy,
    mode: FactoryMode,
    policy: BaselineComparisonPolicy,
    maximum_complexity_increase: Decimal,
) -> tuple[str, ...]:
    baseline_evaluation = baseline.evaluation
    challenger_evaluation = challenger.evaluation
    if baseline_evaluation is None or challenger_evaluation is None:
        return ("BASELINE_COMPARISON_MISSING_EVALUATION",)
    baseline_pf = _metric(baseline_evaluation.metrics, "profit_factor")
    challenger_pf = _metric(challenger_evaluation.metrics, "profit_factor")
    baseline_dd = _metric(baseline_evaluation.metrics, "max_drawdown")
    challenger_dd = _metric(challenger_evaluation.metrics, "max_drawdown")
    baseline_trades = _metric(baseline_evaluation.metrics, "trade_count")
    challenger_trades = _metric(challenger_evaluation.metrics, "trade_count")
    if None in {
        baseline_pf,
        challenger_pf,
        baseline_dd,
        challenger_dd,
        baseline_trades,
        challenger_trades,
    }:
        return ("BASELINE_COMPARISON_METRIC_MISSING",)
    assert baseline_pf is not None
    assert challenger_pf is not None
    assert baseline_dd is not None
    assert challenger_dd is not None
    assert baseline_trades is not None
    assert challenger_trades is not None
    failures: list[str] = []
    minimum_trades = baseline_trades * policy.minimum_trade_count_ratio
    if challenger_trades < minimum_trades:
        failures.append("BASELINE_TRADE_COUNT_DEGRADATION")
    if mode is FactoryMode.ADAPT:
        if challenger_pf < baseline_pf * policy.adapt_minimum_profit_factor_ratio:
            failures.append("BASELINE_PROFIT_FACTOR_DEGRADATION")
        if challenger_dd > baseline_dd * policy.adapt_maximum_drawdown_ratio:
            failures.append("BASELINE_DRAWDOWN_DEGRADATION")
    else:
        if challenger_pf < baseline_pf:
            failures.append("NO_PROFIT_FACTOR_IMPROVEMENT")
        if challenger_dd > baseline_dd:
            failures.append("NO_DRAWDOWN_IMPROVEMENT")
        if (
            challenger_evaluation.complexity
            > baseline_evaluation.complexity + maximum_complexity_increase
        ):
            failures.append("IMPROVEMENT_COMPLEXITY_LIMIT_EXCEEDED")
        improved = (
            challenger_pf > baseline_pf + policy.improvement_epsilon
            or challenger_dd < baseline_dd - policy.improvement_epsilon
        )
        if not improved:
            failures.append("NO_MEASURABLE_BASELINE_IMPROVEMENT")
    return tuple(failures)


def _metric(metrics: Mapping[str, object], name: str) -> Decimal | None:
    value = metrics.get(name)
    if value is None or isinstance(value, bool):
        return None
    try:
        rendered = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return rendered if rendered.is_finite() else None
