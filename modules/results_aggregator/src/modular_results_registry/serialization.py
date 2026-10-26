from __future__ import annotations

from datetime import datetime
from typing import Any, Mapping

from .errors import ValidationError
from .models import (
    ArtifactRecord,
    ArtifactStatus,
    AuditEvidenceRecord,
    CandidateRole,
    ConfigurationRecord,
    DatasetRecord,
    EvidenceStatus,
    ExperimentRecord,
    ExperimentStatus,
    MetricDefinitionRecord,
    MetricSnapshot,
    ObjectiveDirection,
    RegistrationPackage,
    RoleDecision,
    RunRecord,
    RunStatus,
    Severity,
)


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ValidationError(f"{name} must be a mapping")
    return value


def _exact_fields(value: Mapping[str, Any], name: str, fields: frozenset[str]) -> None:
    if set(value) != fields:
        raise ValidationError(f"{name} fields are invalid")


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be a string")
    return value


def _tuple_strings(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, str) for item in value):
        raise ValidationError(f"{name} must be a string list")
    return tuple(value)


def _enum(enum_type: type[Any], value: Any, name: str) -> Any:
    raw = _string(value, name)
    try:
        return enum_type(raw)
    except ValueError as exc:
        raise ValidationError(f"{name} is invalid") from exc


def _datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be an ISO timestamp")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} is not a valid ISO timestamp") from exc


def artifact_from_mapping(value: Mapping[str, Any]) -> ArtifactRecord:
    _exact_fields(
        value,
        "artifact",
        frozenset(
            {
                "artifact_id",
                "package_id",
                "relative_path",
                "media_type",
                "size_bytes",
                "sha256",
                "producer_module_id",
                "producer_module_version",
                "schema_id",
                "schema_version",
                "status",
                "source_artifact_hash",
                "transform_version",
                "transform_hash",
            }
        ),
    )
    return ArtifactRecord(
        artifact_id=_string(value["artifact_id"], "artifact_id"),
        package_id=_string(value["package_id"], "package_id"),
        relative_path=_string(value["relative_path"], "relative_path"),
        media_type=_string(value["media_type"], "media_type"),
        size_bytes=value["size_bytes"],
        sha256=_string(value["sha256"], "sha256"),
        producer_module_id=_string(value["producer_module_id"], "producer_module_id"),
        producer_module_version=_string(
            value["producer_module_version"], "producer_module_version"
        ),
        schema_id=_string(value["schema_id"], "schema_id"),
        schema_version=_string(value["schema_version"], "schema_version"),
        status=_enum(ArtifactStatus, value["status"], "status"),
        source_artifact_hash=value["source_artifact_hash"],
        transform_version=value["transform_version"],
        transform_hash=value["transform_hash"],
    )


def dataset_from_mapping(value: Mapping[str, Any]) -> DatasetRecord:
    _exact_fields(
        value,
        "dataset",
        frozenset(
            {
                "dataset_id",
                "source",
                "market",
                "market_type",
                "instrument",
                "timeframe",
                "min_timestamp",
                "max_timestamp",
                "row_count",
                "sha256",
                "preprocessing_version",
                "aggregation_version",
                "metadata_hash",
                "funding_data_hash",
                "gap_summary",
                "duplicate_summary",
                "quality_status",
                "timezone_name",
            }
        ),
    )
    return DatasetRecord(
        dataset_id=_string(value["dataset_id"], "dataset_id"),
        source=_string(value["source"], "source"),
        market=_string(value["market"], "market"),
        market_type=_string(value["market_type"], "market_type"),
        instrument=_string(value["instrument"], "instrument"),
        timeframe=_string(value["timeframe"], "timeframe"),
        min_timestamp=_datetime(value["min_timestamp"], "min_timestamp"),
        max_timestamp=_datetime(value["max_timestamp"], "max_timestamp"),
        row_count=value["row_count"],
        sha256=_string(value["sha256"], "sha256"),
        preprocessing_version=_string(value["preprocessing_version"], "preprocessing_version"),
        aggregation_version=_string(value["aggregation_version"], "aggregation_version"),
        metadata_hash=_string(value["metadata_hash"], "metadata_hash"),
        funding_data_hash=value["funding_data_hash"],
        gap_summary=_mapping(value["gap_summary"], "gap_summary"),
        duplicate_summary=_mapping(value["duplicate_summary"], "duplicate_summary"),
        quality_status=_string(value["quality_status"], "quality_status"),
        timezone_name=_string(value["timezone_name"], "timezone_name"),
    )


def configuration_from_mapping(value: Mapping[str, Any]) -> ConfigurationRecord:
    _exact_fields(
        value,
        "configuration",
        frozenset(
            {
                "configuration_id",
                "strategy_id",
                "schema_version",
                "canonical_configuration",
                "configuration_hash",
                "stage",
                "execution_assumptions",
                "risk_settings",
                "cost_settings",
                "funding_settings",
                "creation_source",
                "parent_configuration_id",
                "status",
                "producer_configuration_hash",
            }
        ),
    )
    return ConfigurationRecord(
        configuration_id=_string(value["configuration_id"], "configuration_id"),
        strategy_id=_string(value["strategy_id"], "strategy_id"),
        schema_version=_string(value["schema_version"], "schema_version"),
        canonical_configuration=_mapping(
            value["canonical_configuration"], "canonical_configuration"
        ),
        configuration_hash=_string(value["configuration_hash"], "configuration_hash"),
        stage=_string(value["stage"], "stage"),
        execution_assumptions=_mapping(value["execution_assumptions"], "execution_assumptions"),
        risk_settings=_mapping(value["risk_settings"], "risk_settings"),
        cost_settings=_mapping(value["cost_settings"], "cost_settings"),
        funding_settings=_mapping(value["funding_settings"], "funding_settings"),
        creation_source=_string(value["creation_source"], "creation_source"),
        parent_configuration_id=value["parent_configuration_id"],
        status=_string(value["status"], "status"),
        producer_configuration_hash=value["producer_configuration_hash"],
    )


def experiment_from_mapping(value: Mapping[str, Any]) -> ExperimentRecord:
    _exact_fields(
        value,
        "experiment",
        frozenset(
            {
                "experiment_id",
                "research_id",
                "title",
                "description",
                "strategy_id",
                "strategy_version",
                "commit_sha",
                "branch",
                "working_tree_status",
                "diff_hash",
                "patch_artifact_id",
                "module_versions",
                "dependency_lock_hash",
                "started_at",
                "status",
                "search_method",
                "split_policy",
                "parent_experiment_id",
                "completed_at",
                "tags",
            }
        ),
    )
    completed = value["completed_at"]
    return ExperimentRecord(
        experiment_id=_string(value["experiment_id"], "experiment_id"),
        research_id=_string(value["research_id"], "research_id"),
        title=_string(value["title"], "title"),
        description=_string(value["description"], "description"),
        strategy_id=_string(value["strategy_id"], "strategy_id"),
        strategy_version=_string(value["strategy_version"], "strategy_version"),
        commit_sha=_string(value["commit_sha"], "commit_sha"),
        branch=_string(value["branch"], "branch"),
        working_tree_status=_string(value["working_tree_status"], "working_tree_status"),
        diff_hash=value["diff_hash"],
        patch_artifact_id=value["patch_artifact_id"],
        module_versions=_mapping(value["module_versions"], "module_versions"),
        dependency_lock_hash=_string(value["dependency_lock_hash"], "dependency_lock_hash"),
        started_at=_datetime(value["started_at"], "started_at"),
        status=_enum(ExperimentStatus, value["status"], "status"),
        search_method=_string(value["search_method"], "search_method"),
        split_policy=_string(value["split_policy"], "split_policy"),
        parent_experiment_id=value["parent_experiment_id"],
        completed_at=None if completed is None else _datetime(completed, "completed_at"),
        tags=_tuple_strings(value["tags"], "tags"),
    )


def run_from_mapping(value: Mapping[str, Any]) -> RunRecord:
    _exact_fields(
        value,
        "run",
        frozenset(
            {
                "run_id",
                "experiment_id",
                "stage_id",
                "split_id",
                "scenario_id",
                "configuration_id",
                "dataset_ids",
                "seed",
                "status",
                "started_at",
                "ended_at",
                "duration_seconds",
                "worker_id",
                "machine_id",
                "backtest_engine_version",
                "metric_schema_version",
                "result_hash",
                "producer_data_hash",
                "producer_config_hash",
                "producer_module_hash",
                "symbol",
                "base_timeframe",
                "bars_processed",
                "module_names",
                "artifact_ids",
                "warnings",
                "errors",
            }
        ),
    )
    ended = value["ended_at"]
    return RunRecord(
        run_id=_string(value["run_id"], "run_id"),
        experiment_id=_string(value["experiment_id"], "experiment_id"),
        stage_id=_string(value["stage_id"], "stage_id"),
        split_id=_string(value["split_id"], "split_id"),
        scenario_id=_string(value["scenario_id"], "scenario_id"),
        configuration_id=_string(value["configuration_id"], "configuration_id"),
        dataset_ids=_tuple_strings(value["dataset_ids"], "dataset_ids"),
        seed=value["seed"],
        status=_enum(RunStatus, value["status"], "status"),
        started_at=_datetime(value["started_at"], "started_at"),
        ended_at=None if ended is None else _datetime(ended, "ended_at"),
        duration_seconds=value["duration_seconds"],
        worker_id=_string(value["worker_id"], "worker_id"),
        machine_id=_string(value["machine_id"], "machine_id"),
        backtest_engine_version=_string(
            value["backtest_engine_version"], "backtest_engine_version"
        ),
        metric_schema_version=_string(value["metric_schema_version"], "metric_schema_version"),
        result_hash=_string(value["result_hash"], "result_hash"),
        producer_data_hash=_string(value["producer_data_hash"], "producer_data_hash"),
        producer_config_hash=_string(value["producer_config_hash"], "producer_config_hash"),
        producer_module_hash=_string(value["producer_module_hash"], "producer_module_hash"),
        symbol=_string(value["symbol"], "symbol"),
        base_timeframe=_string(value["base_timeframe"], "base_timeframe"),
        bars_processed=value["bars_processed"],
        module_names=_tuple_strings(value["module_names"], "module_names"),
        artifact_ids=_tuple_strings(value["artifact_ids"], "artifact_ids"),
        warnings=_tuple_strings(value["warnings"], "warnings"),
        errors=_tuple_strings(value["errors"], "errors"),
    )


def metric_definition_from_mapping(
    value: Mapping[str, Any],
) -> MetricDefinitionRecord:
    _exact_fields(
        value,
        "metric definition",
        frozenset(
            {
                "metric_name",
                "definition_version",
                "formula",
                "unit",
                "precision",
                "aggregation_method",
                "missing_value_policy",
                "producer",
                "owner_module",
                "direction",
                "schema_version",
            }
        ),
    )
    direction = value["direction"]
    return MetricDefinitionRecord(
        metric_name=_string(value["metric_name"], "metric_name"),
        definition_version=_string(value["definition_version"], "definition_version"),
        formula=_string(value["formula"], "formula"),
        unit=_string(value["unit"], "unit"),
        precision=value["precision"],
        aggregation_method=_string(value["aggregation_method"], "aggregation_method"),
        missing_value_policy=_string(value["missing_value_policy"], "missing_value_policy"),
        producer=_string(value["producer"], "producer"),
        owner_module=_string(value["owner_module"], "owner_module"),
        direction=None if direction is None else _enum(ObjectiveDirection, direction, "direction"),
        schema_version=_string(value["schema_version"], "schema_version"),
    )


def metric_from_mapping(value: Mapping[str, Any]) -> MetricSnapshot:
    _exact_fields(
        value,
        "metric",
        frozenset(
            {
                "run_id",
                "metric_name",
                "value",
                "unit",
                "precision",
                "definition_version",
                "producer",
                "split",
                "period",
                "scenario",
                "warnings",
                "confidence",
            }
        ),
    )
    return MetricSnapshot(
        run_id=_string(value["run_id"], "run_id"),
        metric_name=_string(value["metric_name"], "metric_name"),
        value=value["value"],
        unit=_string(value["unit"], "unit"),
        precision=value["precision"],
        definition_version=_string(value["definition_version"], "definition_version"),
        producer=_string(value["producer"], "producer"),
        split=_string(value["split"], "split"),
        period=_string(value["period"], "period"),
        scenario=_string(value["scenario"], "scenario"),
        warnings=_tuple_strings(value["warnings"], "warnings"),
        confidence=_mapping(value["confidence"], "confidence"),
    )


def evidence_from_mapping(value: Mapping[str, Any]) -> AuditEvidenceRecord:
    _exact_fields(
        value,
        "evidence",
        frozenset(
            {
                "evidence_id",
                "experiment_id",
                "run_ids",
                "evidence_type",
                "producer",
                "producer_version",
                "status",
                "severity",
                "artifact_ids",
                "created_at",
                "scope",
                "evidence_hash",
                "limitations",
            }
        ),
    )
    return AuditEvidenceRecord(
        evidence_id=_string(value["evidence_id"], "evidence_id"),
        experiment_id=_string(value["experiment_id"], "experiment_id"),
        run_ids=_tuple_strings(value["run_ids"], "run_ids"),
        evidence_type=_string(value["evidence_type"], "evidence_type"),
        producer=_string(value["producer"], "producer"),
        producer_version=_string(value["producer_version"], "producer_version"),
        status=_enum(EvidenceStatus, value["status"], "status"),
        severity=_enum(Severity, value["severity"], "severity"),
        artifact_ids=_tuple_strings(value["artifact_ids"], "artifact_ids"),
        created_at=_datetime(value["created_at"], "created_at"),
        scope=_mapping(value["scope"], "scope"),
        evidence_hash=_string(value["evidence_hash"], "evidence_hash"),
        limitations=_tuple_strings(value["limitations"], "limitations"),
    )


def role_from_mapping(value: Mapping[str, Any]) -> RoleDecision:
    _exact_fields(
        value,
        "role",
        frozenset({"experiment_id", "configuration_id", "role", "reason", "actor"}),
    )
    return RoleDecision(
        experiment_id=_string(value["experiment_id"], "experiment_id"),
        configuration_id=_string(value["configuration_id"], "configuration_id"),
        role=_enum(CandidateRole, value["role"], "role"),
        reason=_string(value["reason"], "reason"),
        actor=_string(value["actor"], "actor"),
    )


def registration_package_from_mapping(value: Mapping[str, Any]) -> RegistrationPackage:
    allowed = {
        "package_id",
        "schema_version",
        "producer_module_id",
        "producer_module_version",
        "artifacts",
        "datasets",
        "configurations",
        "experiments",
        "runs",
        "metrics",
        "metric_definitions",
        "evidence",
        "roles",
    }
    if set(value) != allowed:
        raise ValidationError("registration package fields are invalid")
    for field in (
        "artifacts",
        "datasets",
        "configurations",
        "experiments",
        "runs",
        "metrics",
        "metric_definitions",
        "evidence",
        "roles",
    ):
        if not isinstance(value[field], (list, tuple)):
            raise ValidationError(f"{field} must be a list")
    return RegistrationPackage(
        package_id=_string(value["package_id"], "package_id"),
        schema_version=_string(value["schema_version"], "schema_version"),
        producer_module_id=_string(value["producer_module_id"], "producer_module_id"),
        producer_module_version=_string(
            value["producer_module_version"], "producer_module_version"
        ),
        artifacts=tuple(
            artifact_from_mapping(_mapping(item, "artifact")) for item in value["artifacts"]
        ),
        datasets=tuple(
            dataset_from_mapping(_mapping(item, "dataset")) for item in value["datasets"]
        ),
        configurations=tuple(
            configuration_from_mapping(_mapping(item, "configuration"))
            for item in value["configurations"]
        ),
        experiments=tuple(
            experiment_from_mapping(_mapping(item, "experiment")) for item in value["experiments"]
        ),
        runs=tuple(run_from_mapping(_mapping(item, "run")) for item in value["runs"]),
        metrics=tuple(metric_from_mapping(_mapping(item, "metric")) for item in value["metrics"]),
        metric_definitions=tuple(
            metric_definition_from_mapping(_mapping(item, "metric definition"))
            for item in value["metric_definitions"]
        ),
        evidence=tuple(
            evidence_from_mapping(_mapping(item, "evidence")) for item in value["evidence"]
        ),
        roles=tuple(role_from_mapping(_mapping(item, "role")) for item in value["roles"]),
    )
