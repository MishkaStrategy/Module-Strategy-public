from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from types import MappingProxyType
from typing import Any

from .canonical import canonical_sha256

_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:/-]{0,127}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SpecificationError(ValueError):
    """Raised when a research specification is invalid or ambiguous."""


class SearchMethod(StrEnum):
    SINGLE = "single"
    GRID = "grid"
    RANDOM = "random"
    STAGED = "staged"


class ParameterKind(StrEnum):
    INTEGER = "integer"
    DECIMAL = "decimal"
    CATEGORICAL = "categorical"
    BOOLEAN = "boolean"


class DatasetQualityStatus(StrEnum):
    VALID = "valid"
    VALID_WITH_WARNINGS = "valid_with_warnings"
    INVALID = "invalid"


class SplitMode(StrEnum):
    FIXED = "fixed"
    PERCENTAGE = "percentage"


class WalkForwardMode(StrEnum):
    ROLLING = "rolling"
    ANCHORED = "anchored"


@dataclass(frozen=True, slots=True)
class StagedSearchPolicy:
    train_fractions: tuple[float, ...]
    promotion_counts: tuple[int, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.train_fractions, tuple) or len(self.train_fractions) < 2:
            raise SpecificationError("staged search requires at least two train_fractions")
        normalized: list[float] = []
        for value in self.train_fractions:
            if (
                not isinstance(value, int | float)
                or isinstance(value, bool)
                or not math.isfinite(float(value))
            ):
                raise SpecificationError("staged train_fractions must be finite numbers")
            normalized.append(float(value))
        if any(not 0 < value <= 1 for value in normalized):
            raise SpecificationError("staged train_fractions must be in (0, 1]")
        if any(right <= left for left, right in pairwise(normalized)):
            raise SpecificationError("staged train_fractions must be strictly increasing")
        if normalized[-1] != 1.0:
            raise SpecificationError("the final staged train_fraction must equal 1.0")
        object.__setattr__(self, "train_fractions", tuple(normalized))

        if (
            not isinstance(self.promotion_counts, tuple)
            or len(self.promotion_counts) != len(normalized) - 1
        ):
            raise SpecificationError(
                "promotion_counts must contain one limit for every later staged search phase"
            )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 1
            for value in self.promotion_counts
        ):
            raise SpecificationError("promotion_counts must be positive integers")
        if any(right >= left for left, right in pairwise(self.promotion_counts)):
            raise SpecificationError("promotion_counts must be strictly decreasing")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


class ExperimentStatus(StrEnum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    PLANNED = "PLANNED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CANCELED = "CANCELED"
    COMPLETED = "COMPLETED"
    COMPLETED_WITH_FAILURES = "COMPLETED_WITH_FAILURES"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CORRUPTED = "CORRUPTED"


def _identifier(value: str, name: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise SpecificationError(f"{name} must be a portable identifier")
    return value


def _sha256(value: str, name: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SpecificationError(f"{name} must be a lowercase SHA-256 digest")
    return value


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise SpecificationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) or not key for key in value):
            raise SpecificationError("mapping keys must be non-empty strings")
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True, slots=True)
class TimeRange:
    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "start", _utc(self.start, "time range start"))
        object.__setattr__(self, "end", _utc(self.end, "time range end"))
        if self.end <= self.start:
            raise SpecificationError("time range end must be later than start")


@dataclass(frozen=True, slots=True)
class DatasetLineage:
    dataset_id: str
    source: str
    market_type: str
    instrument: str
    timeframe: str
    period: TimeRange
    row_count: int
    timezone: str
    file_format: str
    file_size: int
    sha256: str
    preprocessing_version: str
    aggregation_version: str
    metadata_version: str
    funding_data_sha256: str | None = None
    known_gaps: int = 0
    known_duplicates: int = 0
    quality_status: DatasetQualityStatus = DatasetQualityStatus.VALID

    def __post_init__(self) -> None:
        for field_name in (
            "dataset_id",
            "source",
            "market_type",
            "instrument",
            "timeframe",
            "preprocessing_version",
            "aggregation_version",
            "metadata_version",
        ):
            _identifier(getattr(self, field_name), field_name)
        _sha256(self.sha256, "dataset sha256")
        if self.funding_data_sha256 is not None:
            _sha256(self.funding_data_sha256, "funding_data_sha256")
        if self.timezone != "UTC":
            raise SpecificationError("dataset timezone must be UTC")
        if self.file_format not in {"csv", "parquet"}:
            raise SpecificationError("dataset file_format must be csv or parquet")
        for name in ("row_count", "file_size", "known_gaps", "known_duplicates"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SpecificationError(f"{name} must be a non-negative integer")
        if self.quality_status is DatasetQualityStatus.INVALID:
            raise SpecificationError("invalid datasets cannot be used in research")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ParameterDimension:
    name: str
    kind: ParameterKind
    values: tuple[Any, ...]

    def __post_init__(self) -> None:
        _identifier(self.name, "parameter name")
        if not isinstance(self.values, tuple) or not self.values:
            raise SpecificationError("parameter values must be a non-empty tuple")
        hashes = tuple(canonical_sha256(value) for value in self.values)
        if len(set(hashes)) != len(hashes):
            raise SpecificationError(f"parameter {self.name} contains duplicate values")
        for value in self.values:
            if self.kind is ParameterKind.INTEGER:
                if not isinstance(value, int) or isinstance(value, bool):
                    raise SpecificationError(f"parameter {self.name} requires integer values")
            elif self.kind is ParameterKind.BOOLEAN:
                if not isinstance(value, bool):
                    raise SpecificationError(f"parameter {self.name} requires boolean values")
            elif self.kind is ParameterKind.DECIMAL:
                if not isinstance(value, Decimal) or not value.is_finite():
                    raise SpecificationError(
                        f"parameter {self.name} requires finite Decimal values"
                    )
            elif self.kind is ParameterKind.CATEGORICAL:
                if not isinstance(value, str) or not value:
                    raise SpecificationError(f"parameter {self.name} requires strings")


@dataclass(frozen=True, slots=True)
class ParameterConstraint:
    left: str
    operator: str
    right_parameter: str | None = None
    right_value: Any = None

    def __post_init__(self) -> None:
        _identifier(self.left, "constraint left parameter")
        if self.operator not in {"<", "<=", "==", "!=", ">=", ">"}:
            raise SpecificationError("unsupported parameter constraint operator")
        if (self.right_parameter is None) == (self.right_value is None):
            raise SpecificationError(
                "constraint must define exactly one of right_parameter or right_value"
            )
        if self.right_parameter is not None:
            _identifier(self.right_parameter, "constraint right parameter")

    def accepts(self, values: Mapping[str, Any]) -> bool:
        if self.left not in values:
            raise SpecificationError(f"constraint references unknown parameter {self.left}")
        right = self.right_value
        if self.right_parameter is not None:
            if self.right_parameter not in values:
                raise SpecificationError(
                    f"constraint references unknown parameter {self.right_parameter}"
                )
            right = values[self.right_parameter]
        left = values[self.left]
        try:
            return {
                "<": left < right,
                "<=": left <= right,
                "==": left == right,
                "!=": left != right,
                ">=": left >= right,
                ">": left > right,
            }[self.operator]
        except TypeError as exc:
            raise SpecificationError("constraint operands are not comparable") from exc


@dataclass(frozen=True, slots=True)
class SearchSpace:
    dimensions: tuple[ParameterDimension, ...]
    constraints: tuple[ParameterConstraint, ...] = ()
    maximum_combinations: int = 100_000

    def __post_init__(self) -> None:
        if not self.dimensions:
            raise SpecificationError("search space requires at least one dimension")
        ordered = tuple(sorted(self.dimensions, key=lambda item: item.name))
        if len({item.name for item in ordered}) != len(ordered):
            raise SpecificationError("search-space parameter names must be unique")
        object.__setattr__(self, "dimensions", ordered)
        object.__setattr__(
            self,
            "constraints",
            tuple(sorted(self.constraints, key=canonical_sha256)),
        )
        names = {item.name for item in ordered}
        for constraint in self.constraints:
            if constraint.left not in names or (
                constraint.right_parameter is not None and constraint.right_parameter not in names
            ):
                raise SpecificationError("constraint references an unknown parameter")
        if (
            not isinstance(self.maximum_combinations, int)
            or isinstance(self.maximum_combinations, bool)
            or self.maximum_combinations < 1
        ):
            raise SpecificationError("maximum_combinations must be a positive integer")

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ChronologicalSplitPolicy:
    mode: SplitMode
    train: TimeRange | None = None
    validation: TimeRange | None = None
    test: TimeRange | None = None
    final_untouched_test: TimeRange | None = None
    train_fraction: float | None = None
    validation_fraction: float | None = None
    test_fraction: float | None = None
    final_fraction: float = 0.0
    gap_seconds: int = 0

    def __post_init__(self) -> None:
        if not isinstance(self.gap_seconds, int) or self.gap_seconds < 0:
            raise SpecificationError("gap_seconds must be a non-negative integer")
        if self.mode is SplitMode.FIXED:
            if self.train is None or self.validation is None or self.test is None:
                raise SpecificationError("fixed split requires train, validation and test ranges")
            if any(
                value is not None
                for value in (
                    self.train_fraction,
                    self.validation_fraction,
                    self.test_fraction,
                )
            ):
                raise SpecificationError("fixed split cannot define fractions")
        else:
            if any(value is not None for value in (self.train, self.validation, self.test)):
                raise SpecificationError("percentage split cannot define fixed ranges")
            fractions = (
                self.train_fraction,
                self.validation_fraction,
                self.test_fraction,
            )
            if any(value is None or value <= 0 or value >= 1 for value in fractions):
                raise SpecificationError("percentage split fractions must be between zero and one")
            total = sum(value for value in fractions if value is not None) + self.final_fraction
            if abs(total - 1.0) > 1e-12:
                raise SpecificationError("percentage split fractions must sum to one")
            if self.final_fraction < 0 or self.final_fraction >= 1:
                raise SpecificationError("final_fraction must be in [0, 1)")


@dataclass(frozen=True, slots=True)
class WalkForwardPolicy:
    mode: WalkForwardMode
    train_seconds: int
    validation_seconds: int
    oos_seconds: int
    step_seconds: int
    purge_seconds: int = 0
    embargo_seconds: int = 0
    minimum_history_seconds: int = 0

    def __post_init__(self) -> None:
        for name in (
            "train_seconds",
            "validation_seconds",
            "oos_seconds",
            "step_seconds",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise SpecificationError(f"{name} must be a positive integer")
        for name in ("purge_seconds", "embargo_seconds", "minimum_history_seconds"):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise SpecificationError(f"{name} must be a non-negative integer")


@dataclass(frozen=True, slots=True)
class ResearchSpecification:
    research_id: str
    title: str
    description: str
    strategy_id: str
    strategy_version: str
    module_versions: Mapping[str, str]
    base_configuration: Mapping[str, Any]
    search_method: SearchMethod
    search_space: SearchSpace
    datasets: tuple[DatasetLineage, ...]
    instruments: tuple[str, ...]
    market_type: str
    timeframes: tuple[str, ...]
    date_range: TimeRange
    split_policy: ChronologicalSplitPolicy
    staged_search_policy: StagedSearchPolicy | None = None
    walk_forward_policy: WalkForwardPolicy | None = None
    validation_policy: Mapping[str, Any] = field(default_factory=dict)
    execution_cost_scenarios: tuple[Mapping[str, Any], ...] = ()
    robustness_scenarios: tuple[Mapping[str, Any], ...] = ()
    seeds: tuple[int, ...] = (0,)
    resource_limits: Mapping[str, Any] = field(default_factory=dict)
    parallelism: int = 1
    retry_policy: Mapping[str, Any] = field(default_factory=dict)
    resume_policy: Mapping[str, Any] = field(default_factory=dict)
    ranking_objective: Mapping[str, Any] = field(default_factory=dict)
    promotion_criteria: Mapping[str, Any] = field(default_factory=dict)
    artifact_retention: Mapping[str, Any] = field(default_factory=dict)
    schema_version: int = 1

    def __post_init__(self) -> None:
        for name in ("research_id", "strategy_id", "strategy_version", "market_type"):
            _identifier(getattr(self, name), name)
        if not self.title.strip() or not self.description.strip():
            raise SpecificationError("title and description must be non-empty")
        if not self.datasets:
            raise SpecificationError("at least one dataset is required")
        dataset_ids = [item.dataset_id for item in self.datasets]
        if len(set(dataset_ids)) != len(dataset_ids):
            raise SpecificationError("dataset IDs must be unique")
        object.__setattr__(
            self,
            "datasets",
            tuple(sorted(self.datasets, key=lambda item: item.dataset_id)),
        )
        for name in ("instruments", "timeframes"):
            values = getattr(self, name)
            if not values or len(set(values)) != len(values):
                raise SpecificationError(f"{name} must be non-empty and unique")
            for value in values:
                _identifier(value, name)
        if self.search_method is SearchMethod.STAGED:
            if self.staged_search_policy is None:
                raise SpecificationError("staged search requires staged_search_policy")
            if self.walk_forward_policy is not None:
                raise SpecificationError(
                    "staged search and walk-forward cannot be combined in schema version 1"
                )
        elif self.staged_search_policy is not None:
            raise SpecificationError("staged_search_policy is only valid for staged search")
        if not self.seeds:
            raise SpecificationError("at least one seed is required")
        if len(set(self.seeds)) != len(self.seeds):
            raise SpecificationError("seeds must be unique")
        if any(
            not isinstance(seed, int) or isinstance(seed, bool) or seed < 0 for seed in self.seeds
        ):
            raise SpecificationError("seeds must be non-negative integers")
        if (
            not isinstance(self.parallelism, int)
            or isinstance(self.parallelism, bool)
            or self.parallelism < 1
        ):
            raise SpecificationError("parallelism must be a positive integer")
        if self.schema_version != 1:
            raise SpecificationError("unsupported research specification schema_version")
        for name in (
            "module_versions",
            "base_configuration",
            "validation_policy",
            "resource_limits",
            "retry_policy",
            "resume_policy",
            "ranking_objective",
            "promotion_criteria",
            "artifact_retention",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise SpecificationError(f"{name} must be a mapping")
            object.__setattr__(self, name, freeze(value))
        object.__setattr__(
            self,
            "execution_cost_scenarios",
            tuple(freeze(value) for value in self.execution_cost_scenarios),
        )
        object.__setattr__(
            self,
            "robustness_scenarios",
            tuple(freeze(value) for value in self.robustness_scenarios),
        )

    @property
    def fingerprint(self) -> str:
        return canonical_sha256(self)
