from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from types import MappingProxyType
from typing import Any, cast

from .models import SpecificationError, freeze


class MetricDirection(StrEnum):
    MAXIMIZE = "maximize"
    MINIMIZE = "minimize"


def _decimal(value: Any, name: str, *, allow_string: bool = False) -> Decimal:
    if allow_string and isinstance(value, str):
        try:
            rendered = Decimal(value)
        except Exception as exc:
            raise SpecificationError(f"{name} must be numeric") from exc
        if not rendered.is_finite():
            raise SpecificationError(f"{name} must be finite")
        return rendered
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise SpecificationError(f"{name} must be numeric")
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SpecificationError(f"{name} must be finite")
        return Decimal.from_float(value)
    rendered = Decimal(value)
    if not rendered.is_finite():
        raise SpecificationError(f"{name} must be finite")
    return rendered


@dataclass(frozen=True, slots=True)
class MetricObjective:
    metric: str
    direction: MetricDirection
    weight: Decimal = Decimal("1")

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise SpecificationError("objective metric must be non-empty")
        weight = _decimal(self.weight, "objective weight")
        if weight <= 0:
            raise SpecificationError("objective weight must be positive")
        object.__setattr__(self, "weight", weight)


@dataclass(frozen=True, slots=True)
class MetricConstraint:
    metric: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None

    def __post_init__(self) -> None:
        if not self.metric.strip():
            raise SpecificationError("constraint metric must be non-empty")
        if self.minimum is None and self.maximum is None:
            raise SpecificationError("metric constraint requires a minimum or maximum")
        minimum = (
            _decimal(self.minimum, "constraint minimum", allow_string=True)
            if self.minimum is not None
            else None
        )
        maximum = (
            _decimal(self.maximum, "constraint maximum", allow_string=True)
            if self.maximum is not None
            else None
        )
        if minimum is not None and maximum is not None and minimum > maximum:
            raise SpecificationError("metric constraint minimum exceeds maximum")
        object.__setattr__(self, "minimum", minimum)
        object.__setattr__(self, "maximum", maximum)

    def accepts(self, value: Decimal) -> bool:
        return not (
            (self.minimum is not None and value < self.minimum)
            or (self.maximum is not None and value > self.maximum)
        )


@dataclass(frozen=True, slots=True)
class CandidateMetrics:
    configuration_sha256: str
    metrics: Mapping[str, int | float | Decimal]
    source_run_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.configuration_sha256.strip():
            raise SpecificationError("configuration hash must be non-empty")
        converted = {key: _decimal(value, f"metric {key}") for key, value in self.metrics.items()}
        if any(not isinstance(key, str) or not key for key in converted):
            raise SpecificationError("metric names must be non-empty strings")
        object.__setattr__(self, "metrics", MappingProxyType(converted))
        object.__setattr__(self, "source_run_ids", tuple(sorted(set(self.source_run_ids))))


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    rank: int
    configuration_sha256: str
    score: Decimal
    metrics: Mapping[str, Decimal]
    promoted: bool
    source_run_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(freeze(self.metrics))))


@dataclass(frozen=True, slots=True)
class RankingPolicy:
    objectives: tuple[MetricObjective, ...]
    constraints: tuple[MetricConstraint, ...] = ()
    top_k: int | None = None

    def __post_init__(self) -> None:
        if not self.objectives:
            raise SpecificationError("ranking policy requires at least one objective")
        if len({item.metric for item in self.objectives}) != len(self.objectives):
            raise SpecificationError("ranking objective metrics must be unique")
        if self.top_k is not None and self.top_k < 1:
            raise SpecificationError("top_k must be positive")

    @classmethod
    def from_mappings(
        cls,
        objective: Mapping[str, Any],
        criteria: Mapping[str, Any],
    ) -> RankingPolicy:
        if "objectives" in objective:
            raw_objectives = objective["objectives"]
            if not isinstance(raw_objectives, list | tuple):
                raise SpecificationError("objectives must be a sequence")
        else:
            raw_objectives = [objective]
        objectives: list[MetricObjective] = []
        for item in raw_objectives:
            if not isinstance(item, Mapping):
                raise SpecificationError("each ranking objective must be a mapping")
            allowed = {"metric", "direction", "weight"}
            unknown = set(item) - allowed
            if unknown:
                raise SpecificationError(f"unknown ranking objective fields: {sorted(unknown)}")
            try:
                direction = MetricDirection(str(item.get("direction", "maximize")))
            except ValueError as exc:
                raise SpecificationError("ranking direction is unsupported") from exc
            objectives.append(
                MetricObjective(
                    metric=str(item.get("metric", "")),
                    direction=direction,
                    weight=_decimal(item.get("weight", 1), "objective weight", allow_string=True),
                )
            )
        constraints: list[MetricConstraint] = []
        top_k = criteria.get("top_k")
        if top_k is not None and (
            not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 1
        ):
            raise SpecificationError("promotion top_k must be positive")
        minimums = criteria.get("minimums", {})
        maximums = criteria.get("maximums", {})
        if not isinstance(minimums, Mapping) or not isinstance(maximums, Mapping):
            raise SpecificationError("promotion minimums and maximums must be mappings")
        combined: dict[str, dict[str, Any]] = {}
        for metric, value in minimums.items():
            combined.setdefault(str(metric), {})["minimum"] = value
        for metric, value in maximums.items():
            combined.setdefault(str(metric), {})["maximum"] = value
        known = {"top_k", "minimums", "maximums"}
        for key, value in criteria.items():
            if key in known:
                continue
            if key.startswith("minimum_"):
                combined.setdefault(key.removeprefix("minimum_"), {})["minimum"] = value
            elif key.startswith("maximum_"):
                combined.setdefault(key.removeprefix("maximum_"), {})["maximum"] = value
            else:
                raise SpecificationError(f"unknown promotion criterion: {key}")
        for metric in sorted(combined):
            values = combined[metric]
            constraints.append(
                MetricConstraint(
                    metric=metric,
                    minimum=values.get("minimum"),
                    maximum=values.get("maximum"),
                )
            )
        return cls(tuple(objectives), tuple(constraints), top_k)

    def rank(self, candidates: Sequence[CandidateMetrics]) -> tuple[RankedCandidate, ...]:
        if len({item.configuration_sha256 for item in candidates}) != len(candidates):
            raise SpecificationError("candidate configurations must be unique")
        accepted: list[tuple[CandidateMetrics, Decimal]] = []
        required = {item.metric for item in self.objectives} | {
            item.metric for item in self.constraints
        }
        for candidate in candidates:
            metrics = cast(Mapping[str, Decimal], candidate.metrics)
            missing = required - set(metrics)
            if missing:
                raise SpecificationError(
                    "candidate "
                    f"{candidate.configuration_sha256} is missing metrics {sorted(missing)}"
                )
            if not all(
                constraint.accepts(metrics[constraint.metric]) for constraint in self.constraints
            ):
                continue
            score = Decimal(0)
            for objective in self.objectives:
                value = metrics[objective.metric]
                signed = value if objective.direction is MetricDirection.MAXIMIZE else -value
                score += objective.weight * signed
            accepted.append((candidate, score))
        accepted.sort(key=lambda item: (-item[1], item[0].configuration_sha256))
        promoted_count = len(accepted) if self.top_k is None else min(self.top_k, len(accepted))
        return tuple(
            RankedCandidate(
                rank=index + 1,
                configuration_sha256=candidate.configuration_sha256,
                score=score,
                metrics=cast(Mapping[str, Decimal], candidate.metrics),
                promoted=index < promoted_count,
                source_run_ids=candidate.source_run_ids,
            )
            for index, (candidate, score) in enumerate(accepted)
        )
