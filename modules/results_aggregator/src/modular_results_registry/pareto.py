from __future__ import annotations

from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, Mapping

from .canonical import canonical_json_bytes, deterministic_id
from .errors import ValidationError
from .models import (
    MetricDefinitionRecord,
    MissingMetricPolicy,
    ObjectiveDirection,
    nonblank,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class ParetoObjective:
    metric_name: str
    direction: ObjectiveDirection
    epsilon: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        nonblank("metric_name", self.metric_name)
        if not isinstance(self.direction, ObjectiveDirection):
            raise ValidationError("objective direction must be an ObjectiveDirection")
        if not isinstance(self.epsilon, Decimal) or not self.epsilon.is_finite():
            raise ValidationError("objective epsilon must be a finite Decimal")
        if self.epsilon < 0:
            raise ValidationError("objective epsilon cannot be negative")


def objectives_from_metric_definitions(
    definitions: tuple[MetricDefinitionRecord, ...],
    *,
    metric_names: tuple[str, ...] | None = None,
    epsilons: Mapping[str, Decimal] | None = None,
) -> tuple[ParetoObjective, ...]:
    by_name: dict[str, MetricDefinitionRecord] = {}
    for definition in definitions:
        existing = by_name.get(definition.metric_name)
        if existing is not None:
            raise ValidationError(
                f"multiple metric definition versions supplied for {definition.metric_name}"
            )
        by_name[definition.metric_name] = definition
    selected = tuple(sorted(by_name)) if metric_names is None else tuple(sorted(metric_names))
    resolved_epsilons: Mapping[str, Decimal] = {} if epsilons is None else epsilons
    if len(set(selected)) != len(selected):
        raise ValidationError("objective metric names must be unique")
    unknown = tuple(sorted(set(selected) - set(by_name)))
    if unknown:
        raise ValidationError(f"unknown metric definitions: {list(unknown)}")
    unknown_epsilons = tuple(sorted(set(resolved_epsilons) - set(selected)))
    if unknown_epsilons:
        raise ValidationError(
            f"epsilon supplied for non-objective metrics: {list(unknown_epsilons)}"
        )
    objectives: list[ParetoObjective] = []
    for name in selected:
        definition = by_name[name]
        if definition.direction is None:
            raise ValidationError(f"metric definition {name} has no Pareto direction")
        epsilon = resolved_epsilons.get(name, Decimal("0"))
        if not isinstance(epsilon, Decimal) or not epsilon.is_finite():
            raise ValidationError(f"objective epsilon for {name} must be a finite Decimal")
        objectives.append(ParetoObjective(name, definition.direction, epsilon))
    if not objectives:
        raise ValidationError("at least one directed metric definition is required")
    return tuple(objectives)


@dataclass(frozen=True, slots=True)
class ConstraintResult:
    name: str
    passed: bool
    blocking: bool
    actual_value: Any = None
    threshold: Any = None
    reason: str = ""

    def __post_init__(self) -> None:
        nonblank("constraint name", self.name)
        if not isinstance(self.passed, bool) or not isinstance(self.blocking, bool):
            raise ValidationError("constraint passed/blocking must be boolean")
        if not isinstance(self.reason, str):
            raise ValidationError("constraint reason must be a string")
        try:
            canonical_json_bytes({"actual_value": self.actual_value, "threshold": self.threshold})
        except ValidationError as exc:
            raise ValidationError(
                "constraint actual_value/threshold must be canonical JSON values"
            ) from exc


@dataclass(frozen=True, slots=True)
class ParetoCandidate:
    candidate_id: str
    metrics: Mapping[str, int | float | Decimal | None]
    constraints: tuple[ConstraintResult, ...] = ()
    warnings_count: int = 0
    oos_strength: Decimal = Decimal("0")
    robustness_strength: Decimal = Decimal("0")
    parameter_sensitivity: Decimal = Decimal("0")
    complexity: Decimal = Decimal("0")
    trade_count: int = 0
    configuration_hash: str = ""

    def __post_init__(self) -> None:
        nonblank("candidate_id", self.candidate_id)
        if (
            isinstance(self.warnings_count, bool)
            or not isinstance(self.warnings_count, int)
            or isinstance(self.trade_count, bool)
            or not isinstance(self.trade_count, int)
            or self.warnings_count < 0
            or self.trade_count < 0
        ):
            raise ValidationError("counts must be non-negative integers")
        if not isinstance(self.metrics, MappingABC):
            raise ValidationError("metrics must be a mapping")
        if not isinstance(self.constraints, tuple) or not all(
            isinstance(item, ConstraintResult) for item in self.constraints
        ):
            raise ValidationError("constraints must be a tuple of ConstraintResult")
        normalized: dict[str, int | float | Decimal | None] = {}
        for name, value in self.metrics.items():
            nonblank("metric name", name)
            if value is not None:
                if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
                    raise ValidationError(f"metric {name} must be numeric or null")
                decimal = value if isinstance(value, Decimal) else Decimal(str(value))
                if not decimal.is_finite():
                    raise ValidationError(f"non-finite metric {name} for {self.candidate_id}")
            normalized[name] = value
        object.__setattr__(self, "metrics", MappingProxyType(dict(sorted(normalized.items()))))
        for name in (
            "oos_strength",
            "robustness_strength",
            "parameter_sensitivity",
            "complexity",
        ):
            value = getattr(self, name)
            if not isinstance(value, Decimal) or not value.is_finite():
                raise ValidationError(f"{name} must be a finite Decimal")
        validate_sha256("configuration_hash", self.configuration_hash)


@dataclass(frozen=True, slots=True)
class ParetoResult:
    result_id: str
    frontier: tuple[str, ...]
    dominated_by: Mapping[str, tuple[str, ...]]
    excluded: Mapping[str, tuple[str, ...]]
    ordered_eligible: tuple[str, ...]

    def __post_init__(self) -> None:
        nonblank("result_id", self.result_id)
        for name in ("frontier", "ordered_eligible"):
            value = getattr(self, name)
            if not isinstance(value, tuple) or not all(
                isinstance(item, str) and item for item in value
            ):
                raise ValidationError(f"{name} must be a tuple of non-empty strings")
            if len(set(value)) != len(value):
                raise ValidationError(f"{name} must be unique")
        frozen_maps: dict[str, Mapping[str, tuple[str, ...]]] = {}
        for name in ("dominated_by", "excluded"):
            value = getattr(self, name)
            if not isinstance(value, MappingABC):
                raise ValidationError(f"{name} must be a mapping")
            normalized: dict[str, tuple[str, ...]] = {}
            for key, items in value.items():
                nonblank(f"{name} key", key)
                if not isinstance(items, tuple) or not all(
                    isinstance(item, str) and item for item in items
                ):
                    raise ValidationError(f"{name} values must be tuples of non-empty strings")
                if len(set(items)) != len(items):
                    raise ValidationError(f"{name} values must be unique")
                normalized[key] = tuple(sorted(items))
            frozen_maps[name] = MappingProxyType(dict(sorted(normalized.items())))
        eligible = set(self.ordered_eligible)
        frontier = set(self.frontier)
        dominated = set(frozen_maps["dominated_by"])
        excluded = set(frozen_maps["excluded"])
        if frontier & dominated:
            raise ValidationError("frontier and dominated candidates must be disjoint")
        if frontier | dominated != eligible:
            raise ValidationError(
                "frontier and dominated candidates must partition ordered_eligible"
            )
        if excluded & eligible:
            raise ValidationError("excluded candidates cannot be eligible")
        for candidate_id, dominators in frozen_maps["dominated_by"].items():
            if candidate_id in dominators or not set(dominators).issubset(eligible):
                raise ValidationError(
                    "dominated_by references must target other eligible candidates"
                )
        object.__setattr__(self, "dominated_by", frozen_maps["dominated_by"])
        object.__setattr__(self, "excluded", frozen_maps["excluded"])


def _decimal(value: int | float | Decimal | None, *, candidate: str, metric: str) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except InvalidOperation as exc:
        raise ValidationError(f"invalid metric {metric} for {candidate}") from exc
    if not result.is_finite():
        raise ValidationError(f"non-finite metric {metric} for {candidate}")
    return result


def _dominates(
    left: ParetoCandidate,
    right: ParetoCandidate,
    objectives: tuple[ParetoObjective, ...],
) -> bool:
    strictly_better = False
    for objective in objectives:
        left_value = _decimal(
            left.metrics.get(objective.metric_name),
            candidate=left.candidate_id,
            metric=objective.metric_name,
        )
        right_value = _decimal(
            right.metrics.get(objective.metric_name),
            candidate=right.candidate_id,
            metric=objective.metric_name,
        )
        if left_value is None or right_value is None:
            raise ValidationError("dominance called with missing metric")
        if objective.direction is ObjectiveDirection.MAXIMIZE:
            if left_value + objective.epsilon < right_value:
                return False
            if left_value > right_value + objective.epsilon:
                strictly_better = True
        else:
            if left_value - objective.epsilon > right_value:
                return False
            if left_value + objective.epsilon < right_value:
                strictly_better = True
    return strictly_better


def _tie_key(candidate: ParetoCandidate) -> tuple[Any, ...]:
    return (
        candidate.warnings_count,
        -candidate.oos_strength,
        -candidate.robustness_strength,
        candidate.parameter_sensitivity,
        candidate.complexity,
        -candidate.trade_count,
        candidate.configuration_hash,
        candidate.candidate_id,
    )


def rank_pareto(
    candidates: tuple[ParetoCandidate, ...],
    objectives: tuple[ParetoObjective, ...],
    *,
    missing_policy: MissingMetricPolicy = MissingMetricPolicy.EXCLUDE,
) -> ParetoResult:
    if not isinstance(missing_policy, MissingMetricPolicy):
        raise ValidationError("missing_policy must be a MissingMetricPolicy")
    if not candidates:
        raise ValidationError("at least one Pareto candidate is required")
    if not objectives:
        raise ValidationError("at least one Pareto objective is required")
    if len({item.metric_name for item in objectives}) != len(objectives):
        raise ValidationError("Pareto objective names must be unique")
    if len({item.candidate_id for item in candidates}) != len(candidates):
        raise ValidationError("candidate IDs must be unique")
    objectives = tuple(
        sorted(
            objectives,
            key=lambda item: (item.metric_name, item.direction.value, item.epsilon),
        )
    )
    candidates = tuple(sorted(candidates, key=lambda item: item.candidate_id))

    eligible: list[ParetoCandidate] = []
    excluded: dict[str, tuple[str, ...]] = {}
    for candidate in candidates:
        reasons = [item.name for item in candidate.constraints if item.blocking and not item.passed]
        missing = [
            objective.metric_name
            for objective in objectives
            if _decimal(
                candidate.metrics.get(objective.metric_name),
                candidate=candidate.candidate_id,
                metric=objective.metric_name,
            )
            is None
        ]
        if missing and missing_policy is MissingMetricPolicy.ERROR:
            raise ValidationError(
                f"candidate {candidate.candidate_id} has missing objectives: {missing}"
            )
        reasons.extend(f"MISSING_METRIC:{item}" for item in missing)
        if reasons:
            excluded[candidate.candidate_id] = tuple(sorted(reasons))
        else:
            eligible.append(candidate)

    dominated_by: dict[str, tuple[str, ...]] = {}
    frontier: list[ParetoCandidate] = []
    for candidate in eligible:
        dominators = tuple(
            sorted(
                other.candidate_id
                for other in eligible
                if other.candidate_id != candidate.candidate_id
                and _dominates(other, candidate, objectives)
            )
        )
        if dominators:
            dominated_by[candidate.candidate_id] = dominators
        else:
            frontier.append(candidate)
    frontier.sort(key=_tie_key)
    ordered = tuple(item.candidate_id for item in sorted(eligible, key=_tie_key))
    payload = {
        "objectives": objectives,
        "frontier": tuple(item.candidate_id for item in frontier),
        "dominated_by": dominated_by,
        "excluded": excluded,
        "ordered": ordered,
    }
    return ParetoResult(
        result_id=deterministic_id("pareto", payload, digest_chars=48),
        frontier=tuple(item.candidate_id for item in frontier),
        dominated_by=dominated_by,
        excluded=excluded,
        ordered_eligible=ordered,
    )
