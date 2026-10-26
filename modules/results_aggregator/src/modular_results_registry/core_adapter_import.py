from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from .artifact_store import FileLimits, verify_file
from .canonical import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_sha256,
    deterministic_id,
)
from .core_adapter_contract import (
    CoreImportResult,
    CoreRunContext,
    _FILE_SCHEMAS,
    _METRIC_CONTRACT,
    _METRIC_DIRECTIONS,
    _METRIC_FORMULAS,
    _OPTIONAL_FILES,
    _REQUIRED_FILES,
)
from .core_adapter_io import _load_json_object, _require_int, _require_mapping, _require_string
from .errors import IntegrityError, UnsupportedSchemaError, ValidationError
from .models import (
    ArtifactRecord,
    AuditEvidenceRecord,
    EvidenceStatus,
    MetricDefinitionRecord,
    MetricSnapshot,
    RegistrationPackage,
    RoleDecision,
    RunRecord,
    RunStatus,
    Severity,
)


_SUPPORTED_CORE_EXPORT_VERSIONS = frozenset({"0.4.0", "0.4.2"})
_EFFECTIVE_CONFIG_SCHEMA_ID = "modular-backtest.effective-backtest-config"
_EFFECTIVE_CONFIG_SCHEMA_VERSION = "3"
_ENGINE_CONTRACT_FILE_ONLY_V1 = "official-file-only.v1"
_ENGINE_CONTRACT_EXTENDED_REGISTERED_V1 = "official-extended-registered.v1"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MODULE_REVISION_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_BACKTEST_SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,30}USDT$")
_DATASET_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._:-]{2,127}$")
_EFFECTIVE_FIELDS = {
    "app": {
        "initial_capital",
        "requested_timeframes",
        "max_base_history",
        "max_timeframe_history",
        "performance",
        "seed",
        "checkpoint",
        "end_of_test_policy",
        "output_directory_sha256",
    },
    "data_source": {
        "source_kind",
        "path_sha256",
        "storage_root_sha256",
        "symbol",
        "format",
        "market_type",
        "base_timeframe",
        "gap_policy",
        "timestamp_column",
        "start",
        "end",
        "columns",
        "dataset_id",
        "dataset_sha256",
        "dataset_version",
        "registered_mode",
        "stream_batch_size",
    },
    "engine": {
        "initial_cash",
        "one_active_order",
        "one_position",
        "allow_scale_in",
        "close_open_position_at_end",
        "strict_data_validation",
        "strict_module_errors",
        "deterministic_order_ids",
        "strict_feature_contracts",
        "strict_setup_transitions",
        "fees",
        "execution",
        "risk",
        "data",
    },
    "instrument": {
        "symbol",
        "base_asset",
        "quote_asset",
        "market_type",
        "tick_size",
        "quantity_step",
        "min_quantity",
        "max_quantity",
        "min_notional",
        "max_notional",
        "contract_size",
        "price_precision",
        "quantity_precision",
    },
    "module_identity": {
        "configuration_sha256",
        "configuration_path_sha256",
        "configuration",
        "components",
    },
}
_EXECUTION_FIELDS = {
    "slippage_bps",
    "stop_slippage_bps",
    "intrabar_priority",
    "limit_fill_policy",
    "allow_exit_on_entry_bar",
    "allow_limit_stop_on_entry_bar",
    "allow_reentry_on_exit_bar",
    "close_request_timing",
    "target_liquidity",
    "max_fill_fraction_per_bar",
    "max_fill_quantity_per_bar",
    "stop_limit_same_bar_policy",
}
_RISK_FIELDS = {
    "default_risk_fraction",
    "max_risk_fraction",
    "max_notional_multiple",
    "min_notional",
    "min_quantity",
    "quantity_step",
    "min_stop_distance_pct",
    "max_stop_distance_pct",
    "sizing_model",
}
_PINNED_EXECUTION_FIELDS = _EXECUTION_FIELDS - {
    "allow_limit_stop_on_entry_bar",
    "close_request_timing",
}
_PINNED_RISK_FIELDS = _RISK_FIELDS - {"sizing_model"}
_PINNED_ENGINE_DATA_FIELDS = {
    "expected_interval_seconds",
    "gap_policy",
    "timestamp_semantics",
}


def adapt_core_v04_export(
    export_directory: str | Path,
    context: CoreRunContext,
    *,
    limits: FileLimits = FileLimits(),
) -> CoreImportResult:
    root = Path(export_directory)
    if not root.is_dir() or root.is_symlink():
        raise ValidationError("Core export_directory must be a real directory")
    for filename in _REQUIRED_FILES:
        if not (root / filename).exists():
            raise IntegrityError(f"missing required Core artifact: {filename}")

    filenames = tuple(
        name for name in (*_REQUIRED_FILES, *_OPTIONAL_FILES) if (root / name).exists()
    )
    inventory: dict[str, dict[str, Any]] = {}
    for filename in filenames:
        verified = verify_file(root / filename, limits=limits)
        inventory[filename] = {
            "size_bytes": verified.size_bytes,
            "sha256": verified.sha256,
        }

    run_document = _load_json_object(root / "run.json", limits=limits)
    metrics_document = _load_json_object(root / "metrics.json", limits=limits)
    journal_document = _load_json_object(root / "journal.json", limits=limits)

    manifest = _require_mapping(run_document.get("manifest"), "run.manifest")
    core_run_id = _require_string(manifest.get("run_id"), "manifest.run_id")
    kernel_version = _require_string(manifest.get("kernel_version"), "manifest.kernel_version")
    if kernel_version not in _SUPPORTED_CORE_EXPORT_VERSIONS:
        raise UnsupportedSchemaError(f"unsupported Core version: {kernel_version}")
    producer_data_hash = _require_string(manifest.get("data_hash"), "manifest.data_hash")
    producer_config_hash = _require_string(manifest.get("config_hash"), "manifest.config_hash")
    producer_module_hash = _require_string(manifest.get("module_hash"), "manifest.module_hash")
    for name, value in (
        ("data_hash", producer_data_hash),
        ("config_hash", producer_config_hash),
        ("module_hash", producer_module_hash),
    ):
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise IntegrityError(f"manifest.{name} is not SHA-256")
    symbol = _require_string(manifest.get("symbol"), "manifest.symbol")
    base_timeframe = _require_string(manifest.get("base_timeframe"), "manifest.base_timeframe")
    bars = _require_int(manifest.get("bars"), "manifest.bars")
    bars_processed = _require_int(run_document.get("bars_processed"), "run.bars_processed")
    if bars != bars_processed:
        raise IntegrityError("manifest.bars does not match run.bars_processed")

    modules_value = run_document.get("modules")
    if not isinstance(modules_value, list) or any(
        not isinstance(item, str) or not item for item in modules_value
    ):
        raise IntegrityError("run.modules must be a list of non-empty strings")
    module_names = tuple(modules_value)
    if len(set(module_names)) != len(module_names):
        raise IntegrityError("run.modules contains duplicates")

    module_manifests = manifest.get("module_manifests")
    if not isinstance(module_manifests, list):
        raise IntegrityError("manifest.module_manifests must be a list")
    observed_versions: dict[str, str] = {"core": kernel_version}
    for index, raw in enumerate(module_manifests):
        item = _require_mapping(raw, f"module_manifests[{index}]")
        name = _require_string(item.get("name"), f"module_manifests[{index}].name")
        version = _require_string(item.get("version"), f"module_manifests[{index}].version")
        if name in observed_versions:
            raise IntegrityError(f"duplicate module manifest: {name}")
        observed_versions[name] = version
    declared_versions = dict(context.experiment.module_versions)
    for name, version in observed_versions.items():
        if declared_versions.get(name) != version:
            raise IntegrityError(
                f"experiment module version mismatch for {name}: "
                f"declared={declared_versions.get(name)!r}, observed={version!r}"
            )
    if context.configuration.producer_configuration_hash not in {None, producer_config_hash}:
        raise IntegrityError("configuration producer hash does not match Core manifest")
    if not context.datasets:
        raise ValidationError("Core import requires at least one registered dataset")
    if "effective-backtest-config.json" in inventory:
        if "application-run.json" not in inventory:
            raise IntegrityError("effective Backtest config requires application-run.json")
        application_run = _load_json_object(root / "application-run.json", limits=limits)
        if application_run.get("core_run_id") != core_run_id:
            raise IntegrityError("application-run core identity does not match manifest.run_id")
        producer_performance = _require_mapping(
            application_run.get("performance"), "application-run.performance"
        )
        _validate_effective_config(
            root / "effective-backtest-config.json",
            producer_config_hash=producer_config_hash,
            core_run_id=core_run_id,
            experiment_id=context.experiment.experiment_id,
            stage_id=context.stage_id,
            split_id=context.split_id,
            scenario_id=context.scenario_id,
            seed=context.seed,
            datasets=context.datasets,
            metric_split=context.metric_split,
            metric_period=context.metric_period,
            metric_scenario=context.metric_scenario,
            producer_performance=producer_performance,

            manifest_symbol=symbol,
            manifest_base_timeframe=base_timeframe,
            module_manifests=module_manifests,
            module_parameters=manifest.get("module_parameters"),
            initial_equity=_effective_number(
                metrics_document.get("initial_equity"),
                "metrics.initial_equity",
                positive=True,
            ),
        )

    unknown_metrics = sorted(set(metrics_document) - set(_METRIC_CONTRACT))
    missing_metrics = sorted(set(_METRIC_CONTRACT) - set(metrics_document))
    if unknown_metrics or missing_metrics:
        raise UnsupportedSchemaError(
            f"Core metric contract mismatch; unknown={unknown_metrics}, missing={missing_metrics}"
        )
    errors = journal_document.get("errors")
    if not isinstance(errors, list):
        raise IntegrityError("journal.errors must be a list")
    if context.run_status is RunStatus.COMPLETED and errors:
        raise IntegrityError("run cannot be COMPLETED while journal contains errors")

    package_id = deterministic_id(
        "package",
        {
            "producer": "modular-core",
            "version": kernel_version,
            "run_id": core_run_id,
            "experiment_id": context.experiment.experiment_id,
            "configuration_id": context.configuration.configuration_id,
            "datasets": tuple(item.dataset_id for item in context.datasets),
            "inventory": inventory,
        },
        digest_chars=48,
    )

    artifacts: list[ArtifactRecord] = []
    for filename in sorted(inventory):
        media_type, schema_id, schema_version = _FILE_SCHEMAS[filename]
        metadata = inventory[filename]
        artifacts.append(
            ArtifactRecord(
                artifact_id=deterministic_id(
                    "artifact",
                    {
                        "relative_path": filename,
                        "sha256": metadata["sha256"],
                        "schema_id": schema_id,
                        "schema_version": schema_version,
                    },
                    digest_chars=48,
                ),
                package_id=package_id,
                relative_path=filename,
                media_type=media_type,
                size_bytes=int(metadata["size_bytes"]),
                sha256=str(metadata["sha256"]),
                producer_module_id="modular-core",
                producer_module_version=kernel_version,
                schema_id=schema_id,
                schema_version=schema_version,
            )
        )
    artifact_ids = tuple(item.artifact_id for item in artifacts)

    result_hash = canonical_sha256(
        {"core_run_id": core_run_id, "manifest": manifest, "inventory": inventory}
    )
    created_at_raw = manifest.get("created_at")
    if not isinstance(created_at_raw, str):
        raise IntegrityError("manifest.created_at must be an ISO timestamp")
    try:
        started_at = datetime.fromisoformat(created_at_raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise IntegrityError("manifest.created_at is invalid") from exc

    run = RunRecord(
        run_id=core_run_id,
        experiment_id=context.experiment.experiment_id,
        stage_id=context.stage_id,
        split_id=context.split_id,
        scenario_id=context.scenario_id,
        configuration_id=context.configuration.configuration_id,
        dataset_ids=tuple(item.dataset_id for item in context.datasets),
        seed=context.seed,
        status=context.run_status,
        started_at=started_at,
        ended_at=context.observed_completed_at,
        duration_seconds=context.duration_seconds,
        worker_id=context.worker_id,
        machine_id=context.machine_id,
        backtest_engine_version=kernel_version,
        metric_schema_version="core.metrics.v0.4.0",
        result_hash=result_hash,
        producer_data_hash=producer_data_hash,
        producer_config_hash=producer_config_hash,
        producer_module_hash=producer_module_hash,
        symbol=symbol,
        base_timeframe=base_timeframe,
        bars_processed=bars_processed,
        module_names=module_names,
        artifact_ids=artifact_ids,
        errors=tuple(str(item) for item in errors),
    )

    metric_definitions = tuple(
        MetricDefinitionRecord(
            metric_name=metric_name,
            definition_version="core.metrics.v0.4.0",
            formula=_METRIC_FORMULAS[metric_name],
            unit=_METRIC_CONTRACT[metric_name][0],
            precision=_METRIC_CONTRACT[metric_name][1],
            aggregation_method="AUTHORITATIVE_CORE_SNAPSHOT",
            missing_value_policy="PRESERVE_NULL_OR_REJECT_NONFINITE",
            producer="modular_core.audit.calculate_metrics",
            owner_module="modular_core.audit",
            direction=_METRIC_DIRECTIONS.get(metric_name),
        )
        for metric_name in sorted(_METRIC_CONTRACT)
    )
    metrics: list[MetricSnapshot] = []
    for metric_name in sorted(_METRIC_CONTRACT):
        unit, precision = _METRIC_CONTRACT[metric_name]
        value = metrics_document[metric_name]
        if value is not None and not isinstance(value, (bool, int, float, str)):
            raise IntegrityError(f"metric {metric_name} has unsupported value type")
        metrics.append(
            MetricSnapshot(
                run_id=run.run_id,
                metric_name=metric_name,
                value=value,
                unit=unit,
                precision=precision,
                definition_version="core.metrics.v0.4.0",
                producer="modular_core.audit.calculate_metrics",
                split=context.metric_split,
                period=context.metric_period,
                scenario=context.metric_scenario,
            )
        )

    evidence_hash = canonical_sha256(
        {
            "inventory": inventory,
            "producer_data_hash": producer_data_hash,
            "producer_config_hash": producer_config_hash,
            "producer_module_hash": producer_module_hash,
        }
    )
    evidence = AuditEvidenceRecord(
        evidence_id=deterministic_id(
            "evidence",
            {"type": "artifact_integrity", "run_id": run.run_id, "hash": evidence_hash},
            digest_chars=48,
        ),
        experiment_id=context.experiment.experiment_id,
        run_ids=(run.run_id,),
        evidence_type="artifact_integrity",
        producer="modular-results-registry.core-adapter",
        producer_version="0.1.0.dev0",
        status=EvidenceStatus.PASS,
        severity=Severity.INFO,
        artifact_ids=artifact_ids,
        created_at=context.observed_completed_at,
        scope={"run_id": run.run_id, "source": "Core 0.4 export_directory"},
        evidence_hash=evidence_hash,
        limitations=("The adapter validates exported artifacts but does not re-run the backtest.",),
    )
    role = RoleDecision(
        experiment_id=context.experiment.experiment_id,
        configuration_id=context.configuration.configuration_id,
        role=context.role,
        reason=context.role_reason,
        actor="core-v0.4-adapter",
    )
    package = RegistrationPackage(
        package_id=package_id,
        producer_module_id="modular-results-registry.core-adapter",
        producer_module_version="0.1.0.dev0",
        artifacts=tuple(artifacts),
        datasets=context.datasets,
        configurations=(context.configuration,),
        experiments=(context.experiment,),
        runs=(run,),
        metrics=tuple(metrics),
        metric_definitions=metric_definitions,
        evidence=(evidence,),
        roles=(role,),
    )
    return CoreImportResult(package=package, source_inventory=inventory)


def _validate_effective_config(
    path: Path,
    *,
    producer_config_hash: str,
    core_run_id: str,
    experiment_id: str,
    stage_id: str,
    split_id: str,
    scenario_id: str,
    seed: int,
    datasets: tuple[Any, ...],
    metric_split: str,
    metric_period: str,
    metric_scenario: str,
    producer_performance: Mapping[str, Any],

    manifest_symbol: str,
    manifest_base_timeframe: str,
    module_manifests: list[Any],
    module_parameters: Any,
    initial_equity: int | float,
) -> Mapping[str, Any]:
    """Validate the neutral v3 effective-config proof without importing Runner/Factory."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise IntegrityError("effective Backtest config artifact is invalid") from exc
    expected_document_keys = {
        "schema_id",
        "schema_version",
        "context",
        "effective_configuration",
        "projection_sha256",
    }
    if not isinstance(value, dict) or set(value) != expected_document_keys:
        raise IntegrityError("effective Backtest config document fields are invalid")
    if value.get("schema_id") != "modular-backtest.effective-backtest-config":
        raise UnsupportedSchemaError("unsupported effective Backtest config schema")
    if value.get("schema_version") != _EFFECTIVE_CONFIG_SCHEMA_VERSION:
        raise UnsupportedSchemaError("unsupported effective Backtest config version")
    context = value.get("context")
    expected_context_keys = {
        "run_id",
        "engine_run_id",
        "experiment_id",
        "stage_id",
        "stage_role",
        "split_id",
        "dataset_id",
        "dataset_sha256",
        "scenario_id",
        "seed",
        "repetition_index",
        "period_start",
        "period_end",
        "configuration_sha256",
        "execution_performance_mode",
    }
    if not isinstance(context, dict) or set(context) != expected_context_keys:
        raise IntegrityError("effective Backtest config context fields are invalid")
    _validate_effective_context_schema(context)
    if (
        context.get("engine_run_id") != core_run_id
        or context.get("experiment_id") != experiment_id
        or context.get("stage_id") != stage_id
        or context.get("split_id") != split_id
        or context.get("scenario_id") != scenario_id
        or type(context.get("seed")) is not int
        or context.get("seed") != seed
    ):
        raise IntegrityError("effective Backtest config context does not match Core run")
    if context.get("execution_performance_mode") not in (
        "canonical_audit",
        "research_fast",
    ):
        raise IntegrityError("effective Backtest config execution mode is invalid")
    if (
        context["stage_role"] != metric_split
        or f"{context['period_start']}/{context['period_end']}" != metric_period
        or context["scenario_id"] != metric_scenario
    ):
        raise IntegrityError("effective Backtest config labels do not match published Core metrics")
    matching = [item for item in datasets if item.dataset_id == context.get("dataset_id")]
    if len(matching) != 1 or matching[0].sha256 != context.get("dataset_sha256"):
        raise IntegrityError("effective Backtest config dataset does not match import context")
    effective = _validate_effective_configuration_schema(value.get("effective_configuration"))
    expected_performance = effective["app"]["performance"]["values"]
    if expected_performance["mode"] != context["execution_performance_mode"]:
        raise IntegrityError("effective PerformanceConfig mode differs from context")
    if canonical_json_bytes(producer_performance) != canonical_json_bytes(expected_performance):
        raise IntegrityError("effective PerformanceConfig does not match producer evidence")

    _validate_effective_module_parameters(
        effective,
        module_manifests=module_manifests,
        module_parameters=module_parameters,
    )
    if (
        effective["app"]["seed"] != context["seed"]
        or effective["data_source"]["dataset_id"] != context["dataset_id"]
        or effective["data_source"]["dataset_sha256"] != context["dataset_sha256"]
        or effective["data_source"]["start"] != context["period_start"]
        or effective["data_source"]["end"] != context["period_end"]
        or effective["engine"]["initial_cash"] != effective["app"]["initial_capital"]
        or effective["instrument"]["symbol"] != effective["data_source"]["symbol"]
    ):
        raise IntegrityError("effective Backtest config values disagree with context")
    if (
        effective["data_source"]["symbol"] != manifest_symbol
        or effective["instrument"]["symbol"] != manifest_symbol
        or effective["data_source"]["base_timeframe"] != manifest_base_timeframe
    ):
        raise IntegrityError("effective Backtest config instrument disagrees with Core manifest")
    if (
        effective["engine"]["initial_cash"] != initial_equity
        or effective["app"]["initial_capital"] != initial_equity
    ):
        raise IntegrityError(
            "effective Backtest capital disagrees with Core metrics initial equity"
        )
    if effective["engine"]["close_open_position_at_end"] is not (
        effective["app"]["end_of_test_policy"] == "force_close"
    ):
        raise IntegrityError("effective Backtest end-of-test policy disagrees with engine config")
    if (
        effective["engine"]["data"]["expected_interval_seconds"] != 300
        or effective["engine"]["data"]["gap_policy"] != effective["data_source"]["gap_policy"]
    ):
        raise IntegrityError("effective Backtest engine data config disagrees with source config")
    if _effective_core_config_hash(effective["engine"]) != producer_config_hash:
        raise IntegrityError(
            "effective Backtest engine projection does not match Core manifest config_hash"
        )
    digest = value.get("projection_sha256")
    body = {key: item for key, item in value.items() if key != "projection_sha256"}
    try:
        payload = json.dumps(
            body,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntegrityError("effective Backtest config projection is not canonical JSON") from exc
    if (
        not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest) is None
        or hashlib.sha256(payload).hexdigest() != digest
    ):
        raise IntegrityError("effective Backtest config projection checksum does not match")
    return value


def _effective_core_config_hash(engine: Mapping[str, Any]) -> str:
    """Reproduce Core's semantic EngineConfig identity from the neutral projection."""

    normalized = json.loads(
        json.dumps(
            engine, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":")
        )
    )
    execution = normalized["execution"]
    if execution.get("allow_limit_stop_on_entry_bar") is False:
        execution.pop("allow_limit_stop_on_entry_bar")
    if execution.get("close_request_timing") == "next_open":
        execution.pop("close_request_timing")
    risk = normalized["risk"]
    if risk.get("sizing_model") == "stop_distance":
        risk.pop("sizing_model")
    payload = json.dumps(
        normalized,
        ensure_ascii=True,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()

def _validate_effective_module_parameters(
    effective: Mapping[str, Any],
    *,
    module_manifests: list[Any],
    module_parameters: Any,
) -> None:
    """Bind components to Core using names captured from loaded Core modules."""

    identity = effective["module_identity"]
    configured_modules = identity["configuration"]["modules"]
    if not isinstance(module_parameters, list) or len(module_parameters) != len(module_manifests):
        raise IntegrityError("Core manifest module_parameters are missing or incomplete")
    if len(configured_modules) != len(module_manifests):
        raise IntegrityError("effective Backtest modules do not match Core manifest modules")
    core_by_name: dict[str, tuple[Mapping[str, Any], Mapping[str, Any]]] = {}
    for index, (raw_manifest, raw_parameters) in enumerate(
        zip(module_manifests, module_parameters, strict=True)
    ):
        manifest = _require_mapping(raw_manifest, f"module_manifests[{index}]")
        parameters = _require_mapping(raw_parameters, f"module_parameters[{index}]")
        name = _require_string(manifest.get("name"), f"module_manifests[{index}].name")
        if name in core_by_name:
            raise IntegrityError(f"duplicate Core module manifest: {name}")
        core_by_name[name] = (manifest, parameters)

    configured_by_name: dict[str, tuple[int, Mapping[str, Any]]] = {}
    for index, configured in enumerate(configured_modules):
        name = configured.get("core_manifest_name")
        if not isinstance(name, str) or not name:
            raise IntegrityError(f"effective modules[{index}].core_manifest_name is missing")
        if name in configured_by_name:
            raise IntegrityError("effective Core module manifest names contain duplicates")
        configured_by_name[name] = (index, configured)

    if core_by_name.keys() != configured_by_name.keys():
        raise IntegrityError("effective module identities do not match Core manifest")
    for name, (index, configured) in configured_by_name.items():
        manifest, parameters = core_by_name[name]
        for core_field, sidecar_field in (
            ("version", "runtime_component_version"),
            ("api_version", "api_version"),
            ("state_schema_version", "state_schema_version"),
        ):
            if manifest.get(core_field) != configured.get(sidecar_field):
                raise IntegrityError(
                    f"effective Backtest module identity disagrees with Core manifest at index {index}"
                )
        if _effective_json_bytes(configured.get("parameter_schema")) != _effective_json_bytes(
            manifest.get("parameters_schema")
        ):
            raise IntegrityError(
                f"effective Backtest module parameter schema disagrees with Core manifest {name}"
            )
        if _effective_json_bytes(configured.get("parameters")) != _effective_json_bytes(parameters):
            raise IntegrityError(
                f"effective Backtest module parameters disagree with Core manifest {name}"
            )


def _validate_effective_context_schema(context: Mapping[str, Any]) -> None:
    for name in (
        "run_id",
        "engine_run_id",
        "experiment_id",
        "stage_id",
        "stage_role",
        "split_id",
        "dataset_id",
        "scenario_id",
    ):
        _effective_string(context[name], f"context.{name}", nonblank=True)
    for name in ("dataset_sha256", "configuration_sha256"):
        _effective_sha256(context[name], f"context.{name}")
    _effective_integer(context["seed"], "context.seed")
    _effective_integer(context["repetition_index"], "context.repetition_index")
    if context["repetition_index"] < 0:
        raise IntegrityError("effective Backtest context.repetition_index must be non-negative")
    for name in ("period_start", "period_end"):
        _effective_timestamp(context[name], f"context.{name}")
    mode = context["execution_performance_mode"]
    if not isinstance(mode, str) or mode not in {"canonical_audit", "research_fast"}:
        raise IntegrityError("effective Backtest config execution mode is invalid")


def _validate_effective_configuration_schema(value: Any) -> dict[str, Any]:
    effective = _effective_mapping(
        value,
        "effective configuration",
        {*_EFFECTIVE_FIELDS, "engine_contract_profile"},
    )
    profile = effective["engine_contract_profile"]
    if profile == _ENGINE_CONTRACT_FILE_ONLY_V1:
        execution_fields = _PINNED_EXECUTION_FIELDS
        risk_fields = _PINNED_RISK_FIELDS
        engine_data_fields = _PINNED_ENGINE_DATA_FIELDS
    elif profile == _ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        execution_fields = _EXECUTION_FIELDS
        risk_fields = _RISK_FIELDS
        engine_data_fields = {
            "expected_interval_seconds",
            "gap_policy",
            "timestamp_semantics",
            "max_base_history",
        }
    else:
        raise UnsupportedSchemaError("unsupported effective Backtest engine contract profile")
    for group, expected in _EFFECTIVE_FIELDS.items():
        effective[group] = _effective_mapping(effective[group], f"effective {group}", expected)

    app = effective["app"]
    _effective_number(app["initial_capital"], "app.initial_capital", positive=True)
    _effective_optional_integer(app["max_base_history"], "app.max_base_history")
    _effective_optional_integer(app["max_timeframe_history"], "app.max_timeframe_history")
    performance = _effective_mapping(
        app["performance"],
        "app.performance",
        {"schema_id", "schema_version", "values"},
    )
    if (
        performance["schema_id"] != "modular-backtest.performance-config"
        or performance["schema_version"] != "1"
    ):
        raise UnsupportedSchemaError("unsupported effective PerformanceConfig schema")
    performance_values = _effective_mapping(
        performance["values"],
        "app.performance.values",
        {
            "mode",
            "max_memory_mb",
            "max_events",
            "max_skip_ranges",
            "max_diagnostics",
            "max_equity_points",
            "progress_interval_bars",
            "retained_event_window",
            "equity_sampling",
            "equity_every_n_bars",
            "max_base_history",
            "max_timeframe_history",
            "disable_cyclic_gc_in_hot_loop",
        },
    )
    _effective_enum(
        performance_values["mode"], "performance.mode", {"canonical_audit", "research_fast"}
    )
    _effective_enum(
        performance_values["equity_sampling"],
        "performance.equity_sampling",
        {"every_bar", "every_n_bars", "hourly", "daily", "on_change", "on_trade", "hybrid"},
    )
    for key in (
        "max_memory_mb",
        "max_events",
        "max_skip_ranges",
        "max_diagnostics",
        "max_equity_points",
        "max_base_history",
        "max_timeframe_history",
    ):
        _effective_optional_integer(performance_values[key], f"performance.{key}")
        if performance_values[key] is not None and performance_values[key] < 1:
            raise IntegrityError(f"performance.{key} must be positive")
    for key in ("progress_interval_bars", "retained_event_window", "equity_every_n_bars"):
        _effective_integer(performance_values[key], f"performance.{key}")
        if performance_values[key] < 1:
            raise IntegrityError(f"performance.{key} must be positive")
    if not isinstance(performance_values["disable_cyclic_gc_in_hot_loop"], bool):
        raise IntegrityError("performance.disable_cyclic_gc_in_hot_loop must be boolean")
    if (
        app["max_base_history"] != performance_values["max_base_history"]
        or app["max_timeframe_history"] != performance_values["max_timeframe_history"]
    ):
        raise IntegrityError("app history limits differ from effective PerformanceConfig")
    _effective_string_list(app["requested_timeframes"], "app.requested_timeframes")
    if len(set(app["requested_timeframes"])) != len(app["requested_timeframes"]) or not set(
        app["requested_timeframes"]
    ) <= {"15m", "1h", "4h", "1d"}:
        raise IntegrityError("effective Backtest requested timeframes are unsupported")
    _effective_integer(app["seed"], "app.seed")
    checkpoint = _effective_mapping(app["checkpoint"], "app.checkpoint", {"mode", "restart_at_bar"})
    _effective_enum(checkpoint["mode"], "app.checkpoint.mode", {"disabled", "replay"})
    _effective_optional_integer(checkpoint["restart_at_bar"], "app.checkpoint.restart_at_bar")
    if checkpoint["restart_at_bar"] is not None and (
        checkpoint["restart_at_bar"] < 0 or checkpoint["mode"] != "replay"
    ):
        raise IntegrityError("effective Backtest checkpoint restart boundary is invalid")
    _effective_enum(
        app["end_of_test_policy"], "app.end_of_test_policy", {"force_close", "leave_open"}
    )
    _effective_sha256(app["output_directory_sha256"], "app.output_directory_sha256")

    source = effective["data_source"]
    source_kind = source["source_kind"]
    if not isinstance(source_kind, str) or source_kind not in {"file", "registered"}:
        raise IntegrityError("effective Backtest source kind is invalid")
    _effective_optional_sha256(source["path_sha256"], "data_source.path_sha256")
    _effective_optional_sha256(source["storage_root_sha256"], "data_source.storage_root_sha256")
    if source["source_kind"] == "file":
        if source["path_sha256"] is None or source["storage_root_sha256"] is not None:
            raise IntegrityError("effective Backtest file path identity is invalid")
    elif source["path_sha256"] is not None or source["storage_root_sha256"] is None:
        raise IntegrityError("effective Backtest registered path identity is invalid")
    if (
        not isinstance(source["symbol"], str)
        or _BACKTEST_SYMBOL_RE.fullmatch(source["symbol"]) is None
    ):
        raise IntegrityError("effective Backtest data source symbol is invalid")
    _effective_enum(source["market_type"], "data_source.market_type", {"binance-usdt-m-perpetual"})
    _effective_enum(source["base_timeframe"], "data_source.base_timeframe", {"5m"})
    _effective_enum(source["gap_policy"], "data_source.gap_policy", {"allow", "reject"})
    for name in ("market_type", "base_timeframe", "gap_policy"):
        _effective_string(source[name], f"data_source.{name}")
    _effective_string(source["timestamp_column"], "data_source.timestamp_column", nonblank=True)
    _effective_optional_string(source["format"], "data_source.format")
    _effective_optional_string(source["dataset_id"], "data_source.dataset_id")
    _effective_optional_sha256(source["dataset_sha256"], "data_source.dataset_sha256")
    _effective_optional_integer(source["dataset_version"], "data_source.dataset_version")
    _effective_optional_string(source["registered_mode"], "data_source.registered_mode")
    _effective_optional_integer(source["stream_batch_size"], "data_source.stream_batch_size")
    _effective_optional_timestamp(source["start"], "data_source.start")
    _effective_optional_timestamp(source["end"], "data_source.end")
    if source["start"] is not None and source["end"] is not None:
        start = datetime.fromisoformat(source["start"][:-1] + "+00:00")
        end = datetime.fromisoformat(source["end"][:-1] + "+00:00")
        if end <= start:
            raise IntegrityError("effective Backtest data end must be later than start")
    columns = _effective_mapping(
        source["columns"], "data_source.columns", {"open", "high", "low", "close", "volume"}
    )
    for name, item in columns.items():
        _effective_string(item, f"data_source.columns.{name}", nonblank=True)
    if len({source["timestamp_column"], *columns.values()}) != len(columns) + 1:
        raise IntegrityError("effective Backtest data columns must map to distinct fields")
    if source["source_kind"] == "file":
        if (
            source["format"] not in ("csv", "parquet")
            or source["dataset_version"] is not None
            or source["registered_mode"] is not None
            or source["stream_batch_size"] is not None
        ):
            raise IntegrityError("effective Backtest file source fields are inconsistent")
    else:
        if profile == _ENGINE_CONTRACT_FILE_ONLY_V1:
            raise IntegrityError("file-only engine contract cannot describe registered data")
        dataset_id = source["dataset_id"]
        if not isinstance(dataset_id, str) or _DATASET_ID_RE.fullmatch(dataset_id) is None:
            raise IntegrityError("effective Backtest registered dataset identifier is invalid")
        version = source["dataset_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version <= 0:
            raise IntegrityError("effective Backtest registered dataset version is invalid")
        if source["format"] is not None:
            raise IntegrityError("effective Backtest registered source format must be null")
        mode = source["registered_mode"]
        _effective_enum(mode, "data_source.registered_mode", {"legacy_materialized", "streaming"})
        batch_size = source["stream_batch_size"]
        if mode == "streaming":
            if not isinstance(batch_size, int) or isinstance(batch_size, bool) or batch_size <= 0:
                raise IntegrityError("effective Backtest registered stream batch size is invalid")
        elif batch_size is not None:
            raise IntegrityError(
                "effective Backtest materialized mode cannot set stream batch size"
            )

    engine = effective["engine"]
    _effective_number(engine["initial_cash"], "engine.initial_cash", positive=True)
    for name in (
        "one_active_order",
        "one_position",
        "allow_scale_in",
        "close_open_position_at_end",
        "strict_data_validation",
        "strict_module_errors",
        "deterministic_order_ids",
        "strict_feature_contracts",
        "strict_setup_transitions",
    ):
        _effective_boolean(engine[name], f"engine.{name}")
    fees = _effective_mapping(engine["fees"], "engine.fees", {"maker_rate", "taker_rate"})
    for name, item in fees.items():
        _effective_nonnegative_number(item, f"engine.fees.{name}")
    execution = _effective_mapping(engine["execution"], "engine.execution", execution_fields)
    execution_enums = {
        "intrabar_priority": {"stop", "target"},
        "limit_fill_policy": {"at_limit", "price_improvement"},
        "target_liquidity": {"maker", "taker"},
        "stop_limit_same_bar_policy": {"defer", "fill"},
    }
    if profile == _ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        execution_enums["close_request_timing"] = {"next_open", "current_close"}
    for name, allowed in execution_enums.items():
        _effective_enum(execution[name], f"engine.execution.{name}", allowed)
    for name in (
        "allow_exit_on_entry_bar",
        "allow_reentry_on_exit_bar",
    ):
        _effective_boolean(execution[name], f"engine.execution.{name}")
    if profile == _ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _effective_boolean(
            execution["allow_limit_stop_on_entry_bar"],
            "engine.execution.allow_limit_stop_on_entry_bar",
        )
    for name in ("slippage_bps", "stop_slippage_bps"):
        _effective_nonnegative_number(execution[name], f"engine.execution.{name}")
    fill_fraction = _effective_number(
        execution["max_fill_fraction_per_bar"], "engine.execution.max_fill_fraction_per_bar"
    )
    if not 0 < fill_fraction <= 1:
        raise IntegrityError("effective Backtest max fill fraction is outside (0, 1]")
    if execution["max_fill_quantity_per_bar"] is not None:
        _effective_number(
            execution["max_fill_quantity_per_bar"],
            "engine.execution.max_fill_quantity_per_bar",
            positive=True,
        )
    risk = _effective_mapping(engine["risk"], "engine.risk", risk_fields)
    if profile == _ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _effective_enum(
            risk["sizing_model"],
            "engine.risk.sizing_model",
            {"stop_distance", "historical_limit_cost"},
        )
    default_risk = _effective_number(
        risk["default_risk_fraction"], "engine.risk.default_risk_fraction"
    )
    max_risk = _effective_number(risk["max_risk_fraction"], "engine.risk.max_risk_fraction")
    if not 0 < default_risk <= max_risk <= 1:
        raise IntegrityError("effective Backtest risk fractions are invalid")
    _effective_number(
        risk["max_notional_multiple"], "engine.risk.max_notional_multiple", positive=True
    )
    for name in ("min_notional", "min_quantity", "quantity_step", "min_stop_distance_pct"):
        _effective_nonnegative_number(risk[name], f"engine.risk.{name}")
    if risk["max_stop_distance_pct"] is not None:
        max_stop = _effective_number(
            risk["max_stop_distance_pct"], "engine.risk.max_stop_distance_pct", positive=True
        )
        if max_stop < risk["min_stop_distance_pct"]:
            raise IntegrityError("effective Backtest maximum stop distance is below minimum")
    data = _effective_mapping(engine["data"], "engine.data", engine_data_fields)
    _effective_optional_positive_integer(
        data["expected_interval_seconds"], "engine.data.expected_interval_seconds"
    )
    _effective_enum(data["gap_policy"], "engine.data.gap_policy", {"allow", "reject"})
    _effective_enum(data["timestamp_semantics"], "engine.data.timestamp_semantics", {"open"})
    if profile == _ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _effective_optional_positive_integer(
            data["max_base_history"], "engine.data.max_base_history"
        )
        if app["max_base_history"] != data["max_base_history"]:
            raise IntegrityError("effective Backtest base history limits disagree")
    elif app["max_base_history"] is not None:
        raise IntegrityError(
            "file-only effective Backtest config cannot describe a base history limit"
        )

    instrument = effective["instrument"]
    for name in ("symbol", "base_asset", "quote_asset", "market_type"):
        _effective_string(instrument[name], f"instrument.{name}", nonblank=True)
    if instrument["market_type"] != "linear_futures":
        raise IntegrityError("effective Backtest instrument market type is unsupported")
    for name in ("tick_size", "quantity_step", "min_quantity", "min_notional"):
        _effective_nonnegative_number(instrument[name], f"instrument.{name}")
    _effective_number(instrument["contract_size"], "instrument.contract_size", positive=True)
    for name in ("max_quantity", "max_notional"):
        if instrument[name] is not None:
            _effective_number(instrument[name], f"instrument.{name}", positive=True)
    if (
        instrument["max_quantity"] is not None
        and instrument["max_quantity"] < instrument["min_quantity"]
    ):
        raise IntegrityError("effective Backtest instrument maximum quantity is below minimum")
    if (
        instrument["max_notional"] is not None
        and instrument["max_notional"] < instrument["min_notional"]
    ):
        raise IntegrityError("effective Backtest instrument maximum notional is below minimum")
    for name in ("price_precision", "quantity_precision"):
        _effective_optional_integer(instrument[name], f"instrument.{name}")
        if instrument[name] is not None and instrument[name] < 0:
            raise IntegrityError(f"effective Backtest instrument.{name} must be non-negative")

    identity = _effective_mapping(
        effective["module_identity"],
        "module_identity",
        {
            "configuration_sha256",
            "configuration_path_sha256",
            "configuration",
            "components",
        },
    )
    _effective_sha256(identity["configuration_sha256"], "module_identity.configuration_sha256")
    _effective_sha256(
        identity["configuration_path_sha256"], "module_identity.configuration_path_sha256"
    )
    module_config = _effective_mapping(
        identity["configuration"], "module configuration", {"modules", "rendered"}
    )
    modules = module_config["modules"]
    if not isinstance(modules, list) or not modules:
        raise IntegrityError("effective Backtest module configuration must be a non-empty list")
    module_keys = {
        "core_manifest_name",
        "runtime_component_id",
        "runtime_component_version",
        "import_path",
        "api_version",
        "state_schema_version",
        "source_commit_sha",
        "parameter_schema",
        "parameters",
    }
    for index, item in enumerate(modules):
        module = _effective_mapping(item, f"module configuration modules[{index}]", module_keys)
        for name in (
            "core_manifest_name",
            "runtime_component_id",
            "runtime_component_version",
            "import_path",
            "api_version",
            "state_schema_version",
        ):
            _effective_string(module[name], f"modules[{index}].{name}")
        revision = module["source_commit_sha"]
        if not isinstance(revision, str) or _MODULE_REVISION_RE.fullmatch(revision) is None:
            raise IntegrityError(f"modules[{index}].source_commit_sha is invalid")
        _effective_mapping(module["parameter_schema"], f"modules[{index}].parameter_schema")
        _effective_mapping(module["parameters"], f"modules[{index}].parameters")
    core_manifest_names = [module["core_manifest_name"] for module in modules]
    if len(set(core_manifest_names)) != len(core_manifest_names):
        raise IntegrityError("effective Core module manifest names contain duplicates")
    rendered = _effective_mapping(
        module_config["rendered"], "rendered module configuration", {"modules"}
    )
    rendered_modules = rendered["modules"]
    if not isinstance(rendered_modules, list) or len(rendered_modules) != len(modules):
        raise IntegrityError("rendered module configuration does not match identities")
    rendered_ids: list[str] = []
    for index, (module, item) in enumerate(zip(modules, rendered_modules, strict=True)):
        rendered_item = _effective_mapping(
            item, f"rendered modules[{index}]", {"class", "enabled", "parameters"}
        )
        _effective_string(rendered_item["class"], f"rendered modules[{index}].class")
        _effective_boolean(rendered_item["enabled"], f"rendered modules[{index}].enabled")
        _effective_mapping(rendered_item["parameters"], f"rendered modules[{index}].parameters")
        if (
            rendered_item["class"] != module["import_path"]
            or rendered_item["enabled"] is not True
            or _effective_json_bytes(rendered_item["parameters"])
            != _effective_json_bytes(module["parameters"])
        ):
            raise IntegrityError("effective Backtest rendered module identity does not match")
        rendered_ids.append(module["runtime_component_id"])
    components = identity["components"]
    if (
        not isinstance(components, list)
        or any(not isinstance(item, str) or not item for item in components)
        or len(set(components)) != len(components)
        or components != rendered_ids
    ):
        raise IntegrityError("effective Backtest module components do not match configuration")
    try:
        module_payload = json.dumps(
            rendered,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise IntegrityError(
            "effective Backtest rendered module configuration is not canonical JSON"
        ) from exc
    module_digest = hashlib.sha256(module_payload).hexdigest()
    if module_digest != identity["configuration_sha256"]:
        raise IntegrityError("effective Backtest module configuration digest does not match")
    if not engine["one_position"]:
        raise IntegrityError("effective Backtest engine must preserve one-position mode")
    if (
        profile == _ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
        and risk["sizing_model"] == "historical_limit_cost"
        and execution["limit_fill_policy"] != "at_limit"
    ):
        raise IntegrityError("effective Backtest historical sizing requires at-limit execution")
    return effective


def _effective_mapping(value: Any, label: str, keys: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict) or (keys is not None and set(value) != keys):
        raise IntegrityError(f"effective Backtest {label} fields are invalid")
    return value


def _effective_json_bytes(value: Any) -> bytes:
    try:
        return canonical_json_bytes(value)
    except CanonicalizationError as exc:
        raise IntegrityError("effective Backtest module parameters are not canonical JSON") from exc


def _effective_string(value: Any, label: str, *, nonblank: bool = False) -> None:
    if not isinstance(value, str) or not value or (nonblank and value != value.strip()):
        raise IntegrityError(f"effective Backtest {label} must be a non-empty string")


def _effective_optional_string(value: Any, label: str) -> None:
    if value is not None:
        _effective_string(value, label)


def _effective_boolean(value: Any, label: str) -> None:
    if not isinstance(value, bool):
        raise IntegrityError(f"effective Backtest {label} must be boolean")


def _effective_integer(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise IntegrityError(f"effective Backtest {label} must be an integer")


def _effective_optional_integer(value: Any, label: str) -> None:
    if value is not None:
        _effective_integer(value, label)


def _effective_number(value: Any, label: str, *, positive: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise IntegrityError(f"effective Backtest {label} must be a finite number")
    try:
        finite = math.isfinite(float(value))
    except (OverflowError, TypeError, ValueError):
        finite = False
    if not finite:
        raise IntegrityError(f"effective Backtest {label} must be a finite number")
    if positive and value <= 0:
        raise IntegrityError(f"effective Backtest {label} must be positive")
    return value


def _effective_nonnegative_number(value: Any, label: str) -> int | float:
    number = _effective_number(value, label)
    if number < 0:
        raise IntegrityError(f"effective Backtest {label} cannot be negative")
    return number


def _effective_optional_number(value: Any, label: str) -> None:
    if value is not None:
        _effective_number(value, label)


def _effective_optional_positive_integer(value: Any, label: str) -> None:
    _effective_optional_integer(value, label)
    if value is not None and value < 1:
        raise IntegrityError(f"effective Backtest {label} must be positive")


def _effective_string_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise IntegrityError(f"effective Backtest {label} must be a list of non-empty strings")


def _effective_enum(value: Any, label: str, allowed: set[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise IntegrityError(f"effective Backtest {label} is unsupported")


def _effective_sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        raise IntegrityError(f"effective Backtest {label} must be a lowercase SHA-256")


def _effective_optional_sha256(value: Any, label: str) -> None:
    if value is not None:
        _effective_sha256(value, label)


def _effective_timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise IntegrityError(f"effective Backtest {label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise IntegrityError(
            f"effective Backtest {label} must be a canonical UTC timestamp"
        ) from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise IntegrityError(f"effective Backtest {label} must be a canonical UTC timestamp")
    if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != value:
        raise IntegrityError(f"effective Backtest {label} must be a canonical UTC timestamp")


def _effective_optional_timestamp(value: Any, label: str) -> None:
    if value is not None:
        _effective_timestamp(value, label)
