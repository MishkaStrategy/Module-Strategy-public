from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping

from .canonical import canonical_sha256, deterministic_id
from .errors import ValidationError
from .model_common import (
    ArtifactStatus,
    ExperimentStatus,
    RunStatus,
    _freeze_mapping,
    _require_enum,
    _unique,
    finite_nonnegative_number,
    integer_value,
    nonblank,
    nonnegative_integer,
    safe_relative_path,
    utc,
    validate_commit_sha,
    validate_id,
    validate_schema,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class ArtifactRecord:
    artifact_id: str
    package_id: str
    relative_path: str
    media_type: str
    size_bytes: int
    sha256: str
    producer_module_id: str
    producer_module_version: str
    schema_id: str
    schema_version: str
    status: ArtifactStatus = ArtifactStatus.VERIFIED
    source_artifact_hash: str | None = None
    transform_version: str | None = None
    transform_hash: str | None = None

    def __post_init__(self) -> None:
        validate_id("artifact_id", self.artifact_id)
        validate_id("package_id", self.package_id)
        safe_relative_path(self.relative_path)
        nonblank("media_type", self.media_type)
        nonblank("producer_module_id", self.producer_module_id)
        nonblank("producer_module_version", self.producer_module_version)
        validate_schema("schema_id", self.schema_id)
        validate_schema("schema_version", self.schema_version)
        nonnegative_integer("size_bytes", self.size_bytes)
        validate_sha256("sha256", self.sha256)
        if self.source_artifact_hash is not None:
            validate_sha256("source_artifact_hash", self.source_artifact_hash)
        if (self.transform_version is None) != (self.transform_hash is None):
            raise ValidationError("transform_version and transform_hash must be provided together")
        if self.transform_hash is not None:
            validate_sha256("transform_hash", self.transform_hash)
        _require_enum("status", self.status, ArtifactStatus)
        if self.status not in {ArtifactStatus.VERIFIED, ArtifactStatus.REGISTERED}:
            raise ValidationError("only VERIFIED or REGISTERED artifacts can enter the registry")

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class DatasetRecord:
    dataset_id: str
    source: str
    market: str
    market_type: str
    instrument: str
    timeframe: str
    min_timestamp: datetime
    max_timestamp: datetime
    row_count: int
    sha256: str
    preprocessing_version: str
    aggregation_version: str
    metadata_hash: str
    funding_data_hash: str | None = None
    gap_summary: Mapping[str, Any] = field(default_factory=dict)
    duplicate_summary: Mapping[str, Any] = field(default_factory=dict)
    quality_status: str = "UNKNOWN"
    timezone_name: str = "UTC"

    def __post_init__(self) -> None:
        validate_id("dataset_id", self.dataset_id)
        for name in (
            "source",
            "market",
            "market_type",
            "instrument",
            "timeframe",
            "preprocessing_version",
            "aggregation_version",
            "quality_status",
        ):
            nonblank(name, getattr(self, name))
        object.__setattr__(self, "min_timestamp", utc(self.min_timestamp, "min_timestamp"))
        object.__setattr__(self, "max_timestamp", utc(self.max_timestamp, "max_timestamp"))
        if self.max_timestamp < self.min_timestamp:
            raise ValidationError("dataset max_timestamp precedes min_timestamp")
        nonnegative_integer("row_count", self.row_count)
        validate_sha256("sha256", self.sha256)
        validate_sha256("metadata_hash", self.metadata_hash)
        if self.funding_data_hash is not None:
            validate_sha256("funding_data_hash", self.funding_data_hash)
        if self.timezone_name != "UTC":
            raise ValidationError("dataset timezone must be UTC")
        object.__setattr__(self, "gap_summary", _freeze_mapping(self.gap_summary))
        object.__setattr__(self, "duplicate_summary", _freeze_mapping(self.duplicate_summary))

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ConfigurationRecord:
    configuration_id: str
    strategy_id: str
    schema_version: str
    canonical_configuration: Mapping[str, Any]
    configuration_hash: str
    stage: str
    execution_assumptions: Mapping[str, Any] = field(default_factory=dict)
    risk_settings: Mapping[str, Any] = field(default_factory=dict)
    cost_settings: Mapping[str, Any] = field(default_factory=dict)
    funding_settings: Mapping[str, Any] = field(default_factory=dict)
    creation_source: str = "UNKNOWN"
    parent_configuration_id: str | None = None
    status: str = "ACTIVE"
    producer_configuration_hash: str | None = None

    def __post_init__(self) -> None:
        validate_id("configuration_id", self.configuration_id)
        validate_id("strategy_id", self.strategy_id)
        validate_schema("schema_version", self.schema_version)
        nonblank("stage", self.stage)
        nonblank("creation_source", self.creation_source)
        nonblank("status", self.status)
        if self.parent_configuration_id is not None:
            validate_id("parent_configuration_id", self.parent_configuration_id)
        for name in (
            "canonical_configuration",
            "execution_assumptions",
            "risk_settings",
            "cost_settings",
            "funding_settings",
        ):
            object.__setattr__(self, name, _freeze_mapping(getattr(self, name)))
        validate_sha256("configuration_hash", self.configuration_hash)
        if canonical_sha256(self.canonical_configuration) != self.configuration_hash:
            raise ValidationError("configuration_hash does not match canonical_configuration")
        if self.producer_configuration_hash is not None:
            validate_sha256("producer_configuration_hash", self.producer_configuration_hash)

    @classmethod
    def create(
        cls,
        *,
        strategy_id: str,
        schema_version: str,
        canonical_configuration: Mapping[str, Any],
        stage: str,
        **kwargs: Any,
    ) -> "ConfigurationRecord":
        config_hash = canonical_sha256(canonical_configuration)
        return cls(
            configuration_id=deterministic_id(
                "cfg",
                {
                    "strategy_id": strategy_id,
                    "schema_version": schema_version,
                    "configuration_hash": config_hash,
                },
            ),
            strategy_id=strategy_id,
            schema_version=schema_version,
            canonical_configuration=canonical_configuration,
            configuration_hash=config_hash,
            stage=stage,
            **kwargs,
        )

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class ExperimentRecord:
    experiment_id: str
    research_id: str
    title: str
    description: str
    strategy_id: str
    strategy_version: str
    commit_sha: str
    branch: str
    working_tree_status: str
    diff_hash: str | None
    patch_artifact_id: str | None
    module_versions: Mapping[str, str]
    dependency_lock_hash: str
    started_at: datetime
    status: ExperimentStatus
    search_method: str
    split_policy: str
    parent_experiment_id: str | None = None
    completed_at: datetime | None = None
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_enum("status", self.status, ExperimentStatus)
        for name in ("experiment_id", "research_id", "strategy_id"):
            validate_id(name, getattr(self, name))
        if self.parent_experiment_id is not None:
            validate_id("parent_experiment_id", self.parent_experiment_id)
        if self.patch_artifact_id is not None:
            validate_id("patch_artifact_id", self.patch_artifact_id)
        for name in (
            "title",
            "strategy_version",
            "branch",
            "working_tree_status",
            "search_method",
            "split_policy",
        ):
            nonblank(name, getattr(self, name))
        validate_commit_sha(self.commit_sha)
        validate_sha256("dependency_lock_hash", self.dependency_lock_hash)
        if self.diff_hash is not None:
            validate_sha256("diff_hash", self.diff_hash)
        if self.working_tree_status != "CLEAN" and self.patch_artifact_id is None:
            raise ValidationError("dirty working tree requires a patch artifact reference")
        if not isinstance(self.description, str):
            raise ValidationError("description must be a string")
        for module_name, module_version in self.module_versions.items():
            nonblank("module_name", module_name)
            nonblank("module_version", module_version)
        for tag in self.tags:
            nonblank("tag", tag)
        object.__setattr__(self, "module_versions", _freeze_mapping(self.module_versions))
        object.__setattr__(self, "started_at", utc(self.started_at, "started_at"))
        if self.completed_at is not None:
            completed = utc(self.completed_at, "completed_at")
            if completed < self.started_at:
                raise ValidationError("completed_at precedes started_at")
            object.__setattr__(self, "completed_at", completed)
        _unique("tags", self.tags)

    @property
    def reproducibility_status(self) -> str:
        if self.working_tree_status == "CLEAN":
            return "REPRODUCIBLE_SOURCE_CAPTURED"
        return "PATCH_CAPTURED" if self.patch_artifact_id else "NON_RELEASE_REPRODUCIBILITY"

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)


@dataclass(frozen=True, slots=True)
class RunRecord:
    run_id: str
    experiment_id: str
    stage_id: str
    split_id: str
    scenario_id: str
    configuration_id: str
    dataset_ids: tuple[str, ...]
    seed: int
    status: RunStatus
    started_at: datetime
    ended_at: datetime | None
    duration_seconds: float | None
    worker_id: str
    machine_id: str
    backtest_engine_version: str
    metric_schema_version: str
    result_hash: str
    producer_data_hash: str
    producer_config_hash: str
    producer_module_hash: str
    symbol: str
    base_timeframe: str
    bars_processed: int
    module_names: tuple[str, ...]
    artifact_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _require_enum("status", self.status, RunStatus)
        for name in (
            "run_id",
            "experiment_id",
            "stage_id",
            "split_id",
            "scenario_id",
            "configuration_id",
            "worker_id",
            "machine_id",
        ):
            validate_id(name, getattr(self, name))
        for dataset_id in self.dataset_ids:
            validate_id("dataset_id", dataset_id)
        for artifact_id in self.artifact_ids:
            validate_id("artifact_id", artifact_id)
        _unique("dataset_ids", self.dataset_ids, allow_empty=False)
        _unique("artifact_ids", self.artifact_ids)
        integer_value("seed", self.seed)
        validate_schema("metric_schema_version", self.metric_schema_version)
        validate_sha256("result_hash", self.result_hash)
        validate_sha256("producer_data_hash", self.producer_data_hash)
        validate_sha256("producer_config_hash", self.producer_config_hash)
        validate_sha256("producer_module_hash", self.producer_module_hash)
        nonblank("symbol", self.symbol)
        nonblank("base_timeframe", self.base_timeframe)
        nonnegative_integer("bars_processed", self.bars_processed)
        _unique("module_names", self.module_names)
        object.__setattr__(self, "started_at", utc(self.started_at, "started_at"))
        if self.ended_at is not None:
            ended = utc(self.ended_at, "ended_at")
            if ended < self.started_at:
                raise ValidationError("ended_at precedes started_at")
            object.__setattr__(self, "ended_at", ended)
        if self.duration_seconds is not None:
            finite_nonnegative_number("duration_seconds", self.duration_seconds)
        _unique("warnings", self.warnings)
        _unique("errors", self.errors)

    @property
    def content_hash(self) -> str:
        return canonical_sha256(self)
