"""Strict YAML configuration decoding for Strategy Factory requests."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from .contracts import (
    AdaptationPolicy,
    DatasetDescriptor,
    FactoryMode,
    ImprovementPolicy,
    MarketType,
    ResourceBudget,
    StrategyFactoryRequest,
    TimeRange,
)
from .errors import ContractError


def load_request(
    path: str | Path, *, forced_mode: FactoryMode | None = None
) -> StrategyFactoryRequest:
    """Load one strict schema-v1 request from YAML."""

    config_path = Path(path)
    try:
        value = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ContractError(f"cannot load Strategy Factory config: {config_path}") from exc
    root = _mapping(value, "root")
    _only(
        root,
        {
            "schema_version",
            "request_id",
            "mode",
            "symbol",
            "exchange",
            "market_type",
            "dataset",
            "allowed_timeframes",
            "initial_capital",
            "research_risk_fraction",
            "maximum_drawdown_fraction",
            "minimum_trade_count",
            "objectives",
            "budget",
            "random_seed",
            "allowed_strategy_families",
            "forbidden_module_ids",
            "baseline_strategy_id",
            "source_strategy_id",
            "adaptation_policy",
            "improvement_policy",
            "explicit_holdout",
            "created_at",
        },
        "root",
    )
    if root.get("schema_version", 1) != 1:
        raise ContractError("unsupported Strategy Factory config schema_version")
    mode = FactoryMode(root.get("mode", forced_mode.value if forced_mode else "create"))
    if forced_mode is not None and mode is not forced_mode:
        raise ContractError("CLI command and config mode differ")
    dataset = _dataset(_mapping(root.get("dataset"), "dataset"), config_path.parent)
    budget = _budget(_mapping(root.get("budget"), "budget"))
    holdout_value = root.get("explicit_holdout")
    holdout = _time_range(_mapping(holdout_value, "explicit_holdout")) if holdout_value else None
    created_at = _datetime(root.get("created_at", datetime.now().astimezone().isoformat()))
    return StrategyFactoryRequest(
        request_id=_string(root, "request_id"),
        mode=mode,
        symbol=_string(root, "symbol"),
        exchange=_string(root, "exchange"),
        market_type=MarketType(_string(root, "market_type")),
        dataset=dataset,
        allowed_timeframes=_string_tuple(root.get("allowed_timeframes"), "allowed_timeframes"),
        initial_capital=_decimal(root.get("initial_capital"), "initial_capital"),
        research_risk_fraction=_decimal(
            root.get("research_risk_fraction"), "research_risk_fraction"
        ),
        maximum_drawdown_fraction=_decimal(
            root.get("maximum_drawdown_fraction"), "maximum_drawdown_fraction"
        ),
        minimum_trade_count=_integer(root.get("minimum_trade_count"), "minimum_trade_count"),
        objectives=_mapping(root.get("objectives", {}), "objectives"),
        budget=budget,
        random_seed=_integer(root.get("random_seed", 0), "random_seed", allow_zero=True),
        allowed_strategy_families=frozenset(
            _string_tuple(root.get("allowed_strategy_families"), "allowed_strategy_families")
        ),
        forbidden_module_ids=frozenset(
            _string_tuple(root.get("forbidden_module_ids", ()), "forbidden_module_ids")
        ),
        baseline_strategy_id=_optional_string(root.get("baseline_strategy_id")),
        source_strategy_id=_optional_string(root.get("source_strategy_id")),
        adaptation_policy=_adaptation_policy(
            _mapping(root.get("adaptation_policy", {}), "adaptation_policy")
        ),
        improvement_policy=_improvement_policy(
            _mapping(root.get("improvement_policy", {}), "improvement_policy")
        ),
        explicit_holdout=holdout,
        created_at=created_at,
    )


def _adaptation_policy(value: Mapping[str, Any]) -> AdaptationPolicy:
    _only(
        value,
        {
            "schema_version",
            "allow_parameter_adaptation",
            "allowed_parameter_names",
            "neighborhood_points",
            "require_source_baseline_control",
            "allow_direction_reduction",
        },
        "adaptation_policy",
    )
    return AdaptationPolicy(
        allow_parameter_adaptation=_boolean(
            value.get("allow_parameter_adaptation", True),
            "adaptation_policy.allow_parameter_adaptation",
        ),
        allowed_parameter_names=frozenset(
            _string_tuple(
                value.get("allowed_parameter_names", ()),
                "adaptation_policy.allowed_parameter_names",
            )
        ),
        neighborhood_points=_integer(
            value.get("neighborhood_points", 3),
            "adaptation_policy.neighborhood_points",
        ),
        require_source_baseline_control=_boolean(
            value.get("require_source_baseline_control", True),
            "adaptation_policy.require_source_baseline_control",
        ),
        allow_direction_reduction=_boolean(
            value.get("allow_direction_reduction", False),
            "adaptation_policy.allow_direction_reduction",
        ),
        schema_version=_integer(
            value.get("schema_version", 1),
            "adaptation_policy.schema_version",
        ),
    )


def _improvement_policy(value: Mapping[str, Any]) -> ImprovementPolicy:
    _only(
        value,
        {
            "schema_version",
            "allowed_mutations",
            "allowed_parameter_names",
            "neighborhood_points",
            "maximum_runtime_module_changes",
            "maximum_architectures",
            "maximum_complexity_increase",
            "require_baseline_control",
        },
        "improvement_policy",
    )
    return ImprovementPolicy(
        allowed_mutations=frozenset(
            _string_tuple(
                value.get("allowed_mutations", ("parameters",)),
                "improvement_policy.allowed_mutations",
            )
        ),
        allowed_parameter_names=frozenset(
            _string_tuple(
                value.get("allowed_parameter_names", ()),
                "improvement_policy.allowed_parameter_names",
            )
        ),
        neighborhood_points=_integer(
            value.get("neighborhood_points", 3),
            "improvement_policy.neighborhood_points",
        ),
        maximum_runtime_module_changes=_integer(
            value.get("maximum_runtime_module_changes", 0),
            "improvement_policy.maximum_runtime_module_changes",
            allow_zero=True,
        ),
        maximum_architectures=_integer(
            value.get("maximum_architectures", 8),
            "improvement_policy.maximum_architectures",
        ),
        maximum_complexity_increase=_decimal(
            value.get("maximum_complexity_increase", "0"),
            "improvement_policy.maximum_complexity_increase",
        ),
        require_baseline_control=_boolean(
            value.get("require_baseline_control", True),
            "improvement_policy.require_baseline_control",
        ),
        schema_version=_integer(
            value.get("schema_version", 1),
            "improvement_policy.schema_version",
        ),
    )


def _dataset(value: Mapping[str, Any], base: Path) -> DatasetDescriptor:
    _only(
        value,
        {
            "schema_version",
            "dataset_id",
            "source",
            "symbol",
            "exchange",
            "market_type",
            "base_timeframe",
            "available_timeframes",
            "period",
            "timezone",
            "file_format",
            "uri",
            "sha256",
            "row_count",
            "received_at",
            "funding_uri",
            "funding_sha256",
            "exchange_info_sha256",
        },
        "dataset",
    )
    uri = _string(value, "uri")
    if not uri.startswith("file://") and "://" not in uri:
        uri = str((base / uri).resolve())
    funding_uri = _optional_string(value.get("funding_uri"))
    if funding_uri and not funding_uri.startswith("file://") and "://" not in funding_uri:
        funding_uri = str((base / funding_uri).resolve())
    return DatasetDescriptor(
        dataset_id=_string(value, "dataset_id"),
        source=_string(value, "source"),
        symbol=_string(value, "symbol"),
        exchange=_string(value, "exchange"),
        market_type=MarketType(_string(value, "market_type")),
        base_timeframe=_string(value, "base_timeframe"),
        available_timeframes=_string_tuple(
            value.get("available_timeframes"), "available_timeframes"
        ),
        period=_time_range(_mapping(value.get("period"), "dataset.period")),
        timezone=_string(value, "timezone"),
        file_format=_string(value, "file_format"),
        uri=uri,
        sha256=_string(value, "sha256"),
        row_count=_integer(value.get("row_count"), "row_count"),
        received_at=_datetime(value.get("received_at")),
        funding_uri=funding_uri,
        funding_sha256=_optional_string(value.get("funding_sha256")),
        exchange_info_sha256=_optional_string(value.get("exchange_info_sha256")),
        schema_version=_integer(value.get("schema_version", 1), "dataset schema_version"),
    )


def _budget(value: Mapping[str, Any]) -> ResourceBudget:
    _only(
        value,
        {
            "schema_version",
            "maximum_experiments",
            "maximum_architectures",
            "maximum_finalists",
            "maximum_duration_seconds",
            "maximum_memory_bytes",
            "maximum_cpu_workers",
            "maximum_parallel_tasks",
            "stage_experiment_limits",
        },
        "budget",
    )
    limits = _mapping(value.get("stage_experiment_limits", {}), "stage_experiment_limits")
    return ResourceBudget(
        maximum_experiments=_integer(value.get("maximum_experiments"), "maximum_experiments"),
        maximum_architectures=_integer(value.get("maximum_architectures"), "maximum_architectures"),
        maximum_finalists=_integer(value.get("maximum_finalists"), "maximum_finalists"),
        maximum_duration_seconds=_integer(
            value.get("maximum_duration_seconds"), "maximum_duration_seconds"
        ),
        maximum_memory_bytes=_integer(value.get("maximum_memory_bytes"), "maximum_memory_bytes"),
        maximum_cpu_workers=_integer(value.get("maximum_cpu_workers"), "maximum_cpu_workers"),
        maximum_parallel_tasks=_integer(
            value.get("maximum_parallel_tasks"), "maximum_parallel_tasks"
        ),
        stage_experiment_limits={
            str(key): _integer(item, f"stage limit {key}") for key, item in limits.items()
        },
        schema_version=_integer(value.get("schema_version", 1), "budget schema_version"),
    )


def _time_range(value: Mapping[str, Any]) -> TimeRange:
    _only(value, {"start", "end"}, "time range")
    return TimeRange(start=_datetime(value.get("start")), end=_datetime(value.get("end")))


def _mapping(value: object, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not all(isinstance(key, str) for key in value):
        raise ContractError(f"{name} must be a mapping with string keys")
    return value


def _only(value: Mapping[str, Any], allowed: set[str], name: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"unknown {name} keys: {', '.join(unknown)}")


def _string(value: Mapping[str, Any], key: str) -> str:
    raw = value.get(key)
    if not isinstance(raw, str) or not raw.strip():
        raise ContractError(f"{key} must be a non-empty string")
    return raw.strip()


def _optional_string(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractError("optional string must be non-empty when provided")
    return value.strip()


def _string_tuple(value: object, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ContractError(f"{name} must be a sequence")
    output: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ContractError(f"{name} must contain non-empty strings")
        output.append(item.strip())
    return tuple(output)


def _integer(value: object, name: str, *, allow_zero: bool = False) -> int:
    minimum = 0 if allow_zero else 1
    if not isinstance(value, int) or isinstance(value, bool) or value < minimum:
        raise ContractError(f"{name} must be {'non-negative' if allow_zero else 'positive'}")
    return value


def _boolean(value: object, name: str) -> bool:
    if not isinstance(value, bool):
        raise ContractError(f"{name} must be boolean")
    return value


def _decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise ContractError(f"{name} must be a decimal")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ContractError(f"{name} must be a decimal") from exc
    if not result.is_finite():
        raise ContractError(f"{name} must be finite")
    return result


def _datetime(value: object) -> datetime:
    if not isinstance(value, str):
        raise ContractError("datetime must be an ISO-8601 string")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ContractError("invalid ISO-8601 datetime") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ContractError("datetime must include a timezone")
    return result
