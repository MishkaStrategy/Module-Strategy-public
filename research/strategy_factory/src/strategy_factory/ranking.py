"""Explainable Pareto ranking over Results Aggregator candidate evidence."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from .contracts import CandidateEvaluation, CandidateStrategy
from .errors import ContractError


class ObjectiveDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


@dataclass(frozen=True, slots=True)
class Objective:
    metric: str
    direction: ObjectiveDirection
    epsilon: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        if not self.metric:
            raise ContractError("objective metric cannot be empty")
        if self.epsilon < 0:
            raise ContractError("objective epsilon must be non-negative")


@dataclass(frozen=True, slots=True)
class Constraint:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.metric or self.minimum is None and self.maximum is None:
            raise ContractError("constraint must define a metric and bound")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ContractError("constraint minimum exceeds maximum")


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    candidate: CandidateStrategy
    pareto_front: int
    constraint_results: dict[str, bool]
    objective_values: dict[str, Decimal]
    selection_reasons: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RankingResult:
    ranked: tuple[RankedCandidate, ...]
    rejected: tuple[RankedCandidate, ...]


class ParetoRanker:
    """Rank candidates without collapsing evidence into an opaque scalar."""

    def __init__(
        self,
        *,
        objectives: tuple[Objective, ...],
        constraints: tuple[Constraint, ...],
    ) -> None:
        if not objectives:
            raise ContractError("at least one objective is required")
        if len({item.metric for item in objectives}) != len(objectives):
            raise ContractError("objective metrics must be unique")
        self._objectives = objectives
        self._constraints = constraints

    def rank(self, candidates: tuple[CandidateStrategy, ...]) -> RankingResult:
        eligible: list[_WorkingCandidate] = []
        rejected: list[RankedCandidate] = []
        for candidate in candidates:
            if candidate.evaluation is None:
                rejected.append(RankedCandidate(candidate, 0, {}, {}, ("MISSING_EVALUATION",)))
                continue
            constraint_results = self._evaluate_constraints(candidate.evaluation)
            objective_values = self._objective_values(candidate.evaluation)
            reasons: list[str] = []
            if not all(constraint_results.values()):
                reasons.append("CONSTRAINT_FAILURE")
            if len(objective_values) != len(self._objectives):
                reasons.append("MISSING_OBJECTIVE_METRIC")
            if candidate.evaluation.instability_flags:
                reasons.append("INSTABILITY_FLAGS_PRESENT")
            if reasons:
                rejected.append(
                    RankedCandidate(
                        candidate,
                        0,
                        constraint_results,
                        objective_values,
                        tuple(reasons),
                    )
                )
                continue
            eligible.append(_WorkingCandidate(candidate, constraint_results, objective_values))

        fronts: list[list[_WorkingCandidate]] = []
        remaining = list(eligible)
        while remaining:
            front = [
                item
                for item in remaining
                if not any(
                    self._dominates(other.objective_values, item.objective_values)
                    for other in remaining
                    if other is not item
                )
            ]
            if not front:
                raise ContractError("Pareto ranking could not make progress")
            fronts.append(front)
            identities = {id(item) for item in front}
            remaining = [item for item in remaining if id(item) not in identities]

        ranked: list[RankedCandidate] = []
        for index, front in enumerate(fronts, start=1):
            front.sort(key=_simplicity_key)
            for item in front:
                ranked.append(
                    RankedCandidate(
                        candidate=item.candidate,
                        pareto_front=index,
                        constraint_results=item.constraint_results,
                        objective_values=item.objective_values,
                        selection_reasons=(
                            f"PARETO_FRONT_{index}",
                            "SIMPLICITY_TIE_BREAK_APPLIED",
                        ),
                    )
                )
        rejected.sort(key=lambda item: item.candidate.candidate_id)
        return RankingResult(tuple(ranked), tuple(rejected))

    def _evaluate_constraints(self, evaluation: CandidateEvaluation) -> dict[str, bool]:
        output: dict[str, bool] = dict(evaluation.constraints)
        for item in self._constraints:
            raw = evaluation.metrics.get(item.metric)
            value = _decimal(raw)
            if value is None:
                passed = False
            else:
                passed = True
                if item.minimum is not None:
                    passed = value >= item.minimum
                if passed and item.maximum is not None:
                    passed = value <= item.maximum
            output[item.metric] = passed
        return output

    def _objective_values(self, evaluation: CandidateEvaluation) -> dict[str, Decimal]:
        output: dict[str, Decimal] = {}
        for objective in self._objectives:
            value = _decimal(evaluation.metrics.get(objective.metric))
            if value is not None:
                output[objective.metric] = value
        return output

    def _dominates(self, left: dict[str, Decimal], right: dict[str, Decimal]) -> bool:
        no_worse = True
        strictly_better = False
        for objective in self._objectives:
            lvalue = left[objective.metric]
            rvalue = right[objective.metric]
            if objective.direction is ObjectiveDirection.MAXIMIZE:
                if lvalue + objective.epsilon < rvalue:
                    no_worse = False
                if lvalue > rvalue + objective.epsilon:
                    strictly_better = True
            else:
                if lvalue - objective.epsilon > rvalue:
                    no_worse = False
                if lvalue < rvalue - objective.epsilon:
                    strictly_better = True
        return no_worse and strictly_better


@dataclass(slots=True)
class _WorkingCandidate:
    candidate: CandidateStrategy
    constraint_results: dict[str, bool]
    objective_values: dict[str, Decimal]


def _decimal(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        converted = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return converted if converted.is_finite() else None


def _simplicity_key(item: _WorkingCandidate) -> tuple[Decimal, int, str]:
    evaluation = item.candidate.evaluation
    assert evaluation is not None
    return (
        evaluation.complexity,
        len(item.candidate.architecture.modules),
        item.candidate.candidate_id,
    )
