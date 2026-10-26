from __future__ import annotations

import math
from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from .models import (
    ChronologicalSplitPolicy,
    DatasetLineage,
    DatasetQualityStatus,
    ParameterConstraint,
    ParameterDimension,
    ParameterKind,
    ResearchSpecification,
    SearchMethod,
    SearchSpace,
    SpecificationError,
    SplitMode,
    StagedSearchPolicy,
    TimeRange,
    WalkForwardMode,
    WalkForwardPolicy,
)

_TOP_LEVEL = {
    "research_id",
    "title",
    "description",
    "strategy_id",
    "strategy_version",
    "module_versions",
    "base_configuration",
    "search_method",
    "search_space",
    "datasets",
    "instruments",
    "market_type",
    "timeframes",
    "date_range",
    "split_policy",
    "staged_search_policy",
    "walk_forward_policy",
    "validation_policy",
    "execution_cost_scenarios",
    "robustness_scenarios",
    "seeds",
    "resource_limits",
    "parallelism",
    "retry_policy",
    "resume_policy",
    "ranking_objective",
    "promotion_criteria",
    "artifact_retention",
    "schema_version",
}


def _mapping(value: Any, name: str, allowed: set[str] | None = None) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SpecificationError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise SpecificationError(f"{name} keys must be strings")
    if allowed is not None:
        unknown = set(value) - allowed
        if unknown:
            raise SpecificationError(f"unknown {name} fields: {sorted(unknown)}")
    return value


def _require_fields(data: Mapping[str, Any], name: str, required: set[str]) -> None:
    missing = required - set(data)
    if missing:
        raise SpecificationError(f"missing {name} fields: {sorted(missing)}")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise SpecificationError(f"{name} must be a string")
    return value


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpecificationError(f"{name} must be a list")
    return value


def _string_list(value: Any, name: str) -> tuple[str, ...]:
    items = _list(value, name)
    normalized: list[str] = []
    for index, item in enumerate(items):
        normalized.append(_string(item, f"{name}[{index}]"))
    return tuple(normalized)


def _mapping_list(value: Any, name: str) -> tuple[Mapping[str, Any], ...]:
    items = _list(value, name)
    return tuple(_mapping(item, f"{name}[{index}]") for index, item in enumerate(items))


def _enum[EnumT: StrEnum](value: Any, enum_type: type[EnumT], name: str) -> EnumT:
    candidate = _string(value, name)
    try:
        return enum_type(candidate)
    except ValueError as exc:
        raise SpecificationError(f"{name} is unsupported") from exc


def _time(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SpecificationError(f"{name} is not valid ISO-8601") from exc
    else:
        raise SpecificationError(f"{name} must be an ISO-8601 timestamp")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SpecificationError(f"{name} must include a timezone offset")
    return parsed


def _range(value: Any, name: str) -> TimeRange:
    data = _mapping(value, name, {"start", "end"})
    _require_fields(data, name, {"start", "end"})
    return TimeRange(
        _time(data["start"], f"{name}.start"),
        _time(data["end"], f"{name}.end"),
    )


def _integer(value: Any, name: str, default: int | None = None) -> int:
    candidate = default if value is None else value
    if not isinstance(candidate, int) or isinstance(candidate, bool):
        raise SpecificationError(f"{name} must be an integer")
    return candidate


def _finite_float(value: Any, name: str, default: float | None = None) -> float:
    candidate = default if value is None else value
    if (
        not isinstance(candidate, int | float | Decimal)
        or isinstance(candidate, bool)
        or not math.isfinite(float(candidate))
    ):
        raise SpecificationError(f"{name} must be a finite number")
    return float(candidate)


def _dimension(value: Any) -> ParameterDimension:
    data = _mapping(value, "search dimension", {"name", "kind", "values"})
    _require_fields(data, "search dimension", {"name", "kind", "values"})
    values = _list(data["values"], "search dimension values")
    kind = _enum(data["kind"], ParameterKind, "search dimension kind")
    normalized: list[Any] = []
    for item in values:
        if kind is ParameterKind.DECIMAL:
            try:
                decimal_value = Decimal(str(item))
            except Exception as exc:
                raise SpecificationError("decimal search value is invalid") from exc
            if not decimal_value.is_finite():
                raise SpecificationError("decimal search value must be finite")
            normalized.append(decimal_value)
        else:
            normalized.append(item)
    return ParameterDimension(
        _string(data["name"], "search dimension name"),
        kind,
        tuple(normalized),
    )


def _constraint(value: Any) -> ParameterConstraint:
    data = _mapping(
        value,
        "search constraint",
        {"left", "operator", "right_parameter", "right_value"},
    )
    _require_fields(data, "search constraint", {"left", "operator"})
    right_parameter = data.get("right_parameter")
    if right_parameter is not None:
        right_parameter = _string(right_parameter, "search constraint right_parameter")
    return ParameterConstraint(
        left=_string(data["left"], "search constraint left"),
        operator=_string(data["operator"], "search constraint operator"),
        right_parameter=right_parameter,
        right_value=data.get("right_value"),
    )


def _dataset(value: Any) -> DatasetLineage:
    allowed = {
        "dataset_id",
        "source",
        "market_type",
        "instrument",
        "timeframe",
        "period",
        "row_count",
        "timezone",
        "file_format",
        "file_size",
        "sha256",
        "preprocessing_version",
        "aggregation_version",
        "metadata_version",
        "funding_data_sha256",
        "known_gaps",
        "known_duplicates",
        "quality_status",
    }
    required = {
        "dataset_id",
        "source",
        "market_type",
        "instrument",
        "timeframe",
        "period",
        "row_count",
        "timezone",
        "file_format",
        "file_size",
        "sha256",
        "preprocessing_version",
        "aggregation_version",
        "metadata_version",
    }
    data = _mapping(value, "dataset", allowed)
    _require_fields(data, "dataset", required)
    funding_data_sha256 = data.get("funding_data_sha256")
    if funding_data_sha256 is not None:
        funding_data_sha256 = _string(funding_data_sha256, "dataset.funding_data_sha256")
    return DatasetLineage(
        dataset_id=_string(data["dataset_id"], "dataset.dataset_id"),
        source=_string(data["source"], "dataset.source"),
        market_type=_string(data["market_type"], "dataset.market_type"),
        instrument=_string(data["instrument"], "dataset.instrument"),
        timeframe=_string(data["timeframe"], "dataset.timeframe"),
        period=_range(data["period"], "dataset.period"),
        row_count=_integer(data["row_count"], "dataset.row_count"),
        timezone=_string(data["timezone"], "dataset.timezone"),
        file_format=_string(data["file_format"], "dataset.file_format"),
        file_size=_integer(data["file_size"], "dataset.file_size"),
        sha256=_string(data["sha256"], "dataset.sha256"),
        preprocessing_version=_string(
            data["preprocessing_version"], "dataset.preprocessing_version"
        ),
        aggregation_version=_string(data["aggregation_version"], "dataset.aggregation_version"),
        metadata_version=_string(data["metadata_version"], "dataset.metadata_version"),
        funding_data_sha256=funding_data_sha256,
        known_gaps=_integer(data.get("known_gaps"), "dataset.known_gaps", 0),
        known_duplicates=_integer(
            data.get("known_duplicates"),
            "dataset.known_duplicates",
            0,
        ),
        quality_status=_enum(
            data.get("quality_status", "valid"),
            DatasetQualityStatus,
            "dataset.quality_status",
        ),
    )


def _split(value: Any) -> ChronologicalSplitPolicy:
    allowed = {
        "mode",
        "train",
        "validation",
        "test",
        "final_untouched_test",
        "train_fraction",
        "validation_fraction",
        "test_fraction",
        "final_fraction",
        "gap_seconds",
    }
    data = _mapping(value, "split_policy", allowed)
    _require_fields(data, "split_policy", {"mode"})
    mode = _enum(data["mode"], SplitMode, "split_policy.mode")
    return ChronologicalSplitPolicy(
        mode=mode,
        train=_range(data["train"], "split_policy.train") if "train" in data else None,
        validation=(
            _range(data["validation"], "split_policy.validation") if "validation" in data else None
        ),
        test=_range(data["test"], "split_policy.test") if "test" in data else None,
        final_untouched_test=(
            _range(data["final_untouched_test"], "split_policy.final_untouched_test")
            if "final_untouched_test" in data
            else None
        ),
        train_fraction=(
            _finite_float(data["train_fraction"], "split_policy.train_fraction")
            if "train_fraction" in data
            else None
        ),
        validation_fraction=(
            _finite_float(data["validation_fraction"], "split_policy.validation_fraction")
            if "validation_fraction" in data
            else None
        ),
        test_fraction=(
            _finite_float(data["test_fraction"], "split_policy.test_fraction")
            if "test_fraction" in data
            else None
        ),
        final_fraction=_finite_float(
            data.get("final_fraction"), "split_policy.final_fraction", 0.0
        ),
        gap_seconds=_integer(data.get("gap_seconds"), "split_policy.gap_seconds", 0),
    )


def _staged_search(value: Any) -> StagedSearchPolicy:
    data = _mapping(
        value,
        "staged_search_policy",
        {"train_fractions", "promotion_counts"},
    )
    _require_fields(
        data,
        "staged_search_policy",
        {"train_fractions", "promotion_counts"},
    )
    fractions = _list(data["train_fractions"], "staged_search_policy.train_fractions")
    counts = _list(data["promotion_counts"], "staged_search_policy.promotion_counts")
    return StagedSearchPolicy(
        train_fractions=tuple(fractions),
        promotion_counts=tuple(counts),
    )


def _walk_forward(value: Any) -> WalkForwardPolicy:
    allowed = {
        "mode",
        "train_seconds",
        "validation_seconds",
        "oos_seconds",
        "step_seconds",
        "purge_seconds",
        "embargo_seconds",
        "minimum_history_seconds",
    }
    data = _mapping(value, "walk_forward_policy", allowed)
    _require_fields(
        data,
        "walk_forward_policy",
        {"mode", "train_seconds", "validation_seconds", "oos_seconds", "step_seconds"},
    )
    mode = _enum(data["mode"], WalkForwardMode, "walk_forward_policy.mode")
    return WalkForwardPolicy(
        mode=mode,
        train_seconds=_integer(data["train_seconds"], "walk_forward.train_seconds"),
        validation_seconds=_integer(
            data["validation_seconds"],
            "walk_forward.validation_seconds",
        ),
        oos_seconds=_integer(data["oos_seconds"], "walk_forward.oos_seconds"),
        step_seconds=_integer(data["step_seconds"], "walk_forward.step_seconds"),
        purge_seconds=_integer(data.get("purge_seconds"), "walk_forward.purge_seconds", 0),
        embargo_seconds=_integer(
            data.get("embargo_seconds"),
            "walk_forward.embargo_seconds",
            0,
        ),
        minimum_history_seconds=_integer(
            data.get("minimum_history_seconds"),
            "walk_forward.minimum_history_seconds",
            0,
        ),
    )


def research_specification_from_mapping(value: Any) -> ResearchSpecification:
    data = _mapping(value, "research specification", _TOP_LEVEL)
    required = {
        "research_id",
        "title",
        "description",
        "strategy_id",
        "strategy_version",
        "module_versions",
        "base_configuration",
        "search_method",
        "search_space",
        "datasets",
        "instruments",
        "market_type",
        "timeframes",
        "date_range",
        "split_policy",
    }
    _require_fields(data, "research specification", required)
    search_data = _mapping(
        data["search_space"],
        "search_space",
        {"dimensions", "constraints", "maximum_combinations"},
    )
    _require_fields(search_data, "search_space", {"dimensions"})
    dimensions = search_data["dimensions"]
    constraints = search_data.get("constraints", [])
    if not isinstance(dimensions, list) or not isinstance(constraints, list):
        raise SpecificationError("search-space dimensions and constraints must be lists")
    datasets = _list(data["datasets"], "datasets")
    execution_cost_scenarios = _mapping_list(
        data.get("execution_cost_scenarios", []), "execution_cost_scenarios"
    )
    robustness_scenarios = _mapping_list(
        data.get("robustness_scenarios", []), "robustness_scenarios"
    )
    seeds = _list(data.get("seeds", [0]), "seeds")
    return ResearchSpecification(
        research_id=_string(data["research_id"], "research_id"),
        title=_string(data["title"], "title"),
        description=_string(data["description"], "description"),
        strategy_id=_string(data["strategy_id"], "strategy_id"),
        strategy_version=_string(data["strategy_version"], "strategy_version"),
        module_versions=_mapping(data["module_versions"], "module_versions"),
        base_configuration=_mapping(data["base_configuration"], "base_configuration"),
        search_method=_enum(data["search_method"], SearchMethod, "search_method"),
        search_space=SearchSpace(
            dimensions=tuple(_dimension(item) for item in dimensions),
            constraints=tuple(_constraint(item) for item in constraints),
            maximum_combinations=_integer(
                search_data.get("maximum_combinations"),
                "search_space.maximum_combinations",
                100_000,
            ),
        ),
        datasets=tuple(_dataset(item) for item in datasets),
        instruments=_string_list(data["instruments"], "instruments"),
        market_type=_string(data["market_type"], "market_type"),
        timeframes=_string_list(data["timeframes"], "timeframes"),
        date_range=_range(data["date_range"], "date_range"),
        split_policy=_split(data["split_policy"]),
        staged_search_policy=(
            _staged_search(data["staged_search_policy"])
            if data.get("staged_search_policy") is not None
            else None
        ),
        walk_forward_policy=(
            _walk_forward(data["walk_forward_policy"])
            if data.get("walk_forward_policy") is not None
            else None
        ),
        validation_policy=_mapping(data.get("validation_policy", {}), "validation_policy"),
        execution_cost_scenarios=execution_cost_scenarios,
        robustness_scenarios=robustness_scenarios,
        seeds=tuple(seeds),
        resource_limits=_mapping(data.get("resource_limits", {}), "resource_limits"),
        parallelism=_integer(data.get("parallelism"), "parallelism", 1),
        retry_policy=_mapping(data.get("retry_policy", {}), "retry_policy"),
        resume_policy=_mapping(data.get("resume_policy", {}), "resume_policy"),
        ranking_objective=_mapping(data.get("ranking_objective", {}), "ranking_objective"),
        promotion_criteria=_mapping(data.get("promotion_criteria", {}), "promotion_criteria"),
        artifact_retention=_mapping(data.get("artifact_retention", {}), "artifact_retention"),
        schema_version=_integer(data.get("schema_version"), "schema_version", 1),
    )


def load_research_specification(path: Path) -> ResearchSpecification:
    if path.is_symlink() or not path.is_file():
        raise SpecificationError("research specification must be a regular file")
    try:
        payload = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SpecificationError("cannot read research specification") from exc
    try:
        value = yaml.safe_load(payload)
    except yaml.YAMLError as exc:
        raise SpecificationError("research specification is not valid YAML/JSON") from exc
    return research_specification_from_mapping(value)
