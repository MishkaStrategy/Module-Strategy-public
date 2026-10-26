from __future__ import annotations

import hashlib
import json
import math
import os
import re
import stat
import tempfile
from collections.abc import Mapping, Sequence, Set
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .models import SpecificationError

EFFECTIVE_CONFIG_CONTRACT = "effective-backtest-config.v3"
EFFECTIVE_CONFIG_SCHEMA_ID = "modular-backtest.effective-backtest-config"
EFFECTIVE_CONFIG_SCHEMA_VERSION = "3"
EFFECTIVE_CONFIG_FILENAME = "effective-backtest-config.json"
PERFORMANCE_CONFIG_SCHEMA_ID = "modular-backtest.performance-config"
PERFORMANCE_CONFIG_SCHEMA_VERSION = "1"
ENGINE_CONTRACT_FILE_ONLY_V1 = "official-file-only.v1"
ENGINE_CONTRACT_EXTENDED_REGISTERED_V1 = "official-extended-registered.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SOURCE_REVISION = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_CONTEXT_KEYS = frozenset(
    {
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
)
_PREPARED_CONTEXT_KEYS = _CONTEXT_KEYS - {"engine_run_id"}
_EFFECTIVE_KEYS = frozenset(
    {"engine_contract_profile", "app", "data_source", "engine", "instrument", "module_identity"}
)
_APP_KEYS = frozenset(
    {
        "initial_capital",
        "requested_timeframes",
        "max_base_history",
        "max_timeframe_history",
        "performance",
        "seed",
        "checkpoint",
        "end_of_test_policy",
        "output_directory_sha256",
    }
)
_V2_APP_KEYS = frozenset(
    {
        "initial_capital",
        "requested_timeframes",
        "seed",
        "checkpoint",
        "end_of_test_policy",
        "output_directory_sha256",
    }
)
_PERFORMANCE_VALUE_KEYS = frozenset(
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
    }
)
_CHECKPOINT_KEYS = frozenset({"mode", "restart_at_bar"})
_SOURCE_KEYS = frozenset(
    {
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
    }
)
_COLUMN_KEYS = frozenset({"open", "high", "low", "close", "volume"})
_ENGINE_KEYS = frozenset(
    {
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
    }
)
_PINNED_ENGINE_KEYS = _ENGINE_KEYS
_FEE_KEYS = frozenset({"maker_rate", "taker_rate"})
_EXECUTION_KEYS = frozenset(
    {
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
)
_PINNED_EXECUTION_KEYS = _EXECUTION_KEYS - {"allow_limit_stop_on_entry_bar", "close_request_timing"}
_RISK_KEYS = frozenset(
    {
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
)
_PINNED_RISK_KEYS = _RISK_KEYS - {"sizing_model"}
_ENGINE_DATA_KEYS = frozenset(
    {"expected_interval_seconds", "gap_policy", "timestamp_semantics", "max_base_history"}
)
_PINNED_ENGINE_DATA_KEYS = _ENGINE_DATA_KEYS - {"max_base_history"}
_INSTRUMENT_KEYS = frozenset(
    {
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
    }
)
_MODULE_IDENTITY_KEYS = frozenset(
    {"configuration_sha256", "configuration_path_sha256", "configuration", "components"}
)
_MODULE_CONFIG_KEYS = frozenset({"modules", "rendered"})
_MODULE_KEYS = frozenset(
    {
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
)
_DOCUMENT_KEYS = frozenset(
    {"schema_id", "schema_version", "context", "effective_configuration", "projection_sha256"}
)
_APP_CONFIG_FIELDS = frozenset(
    {
        "data",
        "modules",
        "initial_capital",
        "requested_timeframes",
        "execution",
        "fees",
        "risk",
        "instrument",
        "output",
        "seed",
        "checkpoint",
        "end_of_test_policy",
    }
)
_FILE_ONLY_SOURCE_FIELDS = frozenset(
    {
        "path",
        "format",
        "symbol",
        "market_type",
        "base_timeframe",
        "gap_policy",
        "timestamp_column",
        "start",
        "end",
        "columns",
    }
)
_EXTENDED_SOURCE_FIELDS = frozenset(
    {
        "symbol",
        "path",
        "format",
        "market_type",
        "base_timeframe",
        "gap_policy",
        "timestamp_column",
        "start",
        "end",
        "columns",
        "storage_root",
        "dataset_id",
        "dataset_version",
        "registered_mode",
        "stream_batch_size",
    }
)
_PROFILE_FIELD_SETS: dict[str, dict[str, frozenset[str]]] = {
    ENGINE_CONTRACT_FILE_ONLY_V1: {
        "data_source": _FILE_ONLY_SOURCE_FIELDS,
        "engine": _ENGINE_KEYS,
        "execution": _PINNED_EXECUTION_KEYS,
        "risk": _PINNED_RISK_KEYS,
        "engine_data": _PINNED_ENGINE_DATA_KEYS,
    },
    ENGINE_CONTRACT_EXTENDED_REGISTERED_V1: {
        "data_source": _EXTENDED_SOURCE_FIELDS,
        "engine": _ENGINE_KEYS,
        "execution": _EXECUTION_KEYS,
        "risk": _RISK_KEYS,
        "engine_data": _ENGINE_DATA_KEYS,
    },
}


@dataclass(frozen=True, slots=True)
class EffectiveConfigArtifactReference:
    relative_path: str
    sha256: str
    size_bytes: int
    sidecar_root: str | None = field(default=None, metadata={"canonical_omit_none": True})

    def __post_init__(self) -> None:
        if self.relative_path != EFFECTIVE_CONFIG_FILENAME:
            raise SpecificationError("effective config artifact path is not canonical")
        if not isinstance(self.sha256, str) or _SHA256.fullmatch(self.sha256) is None:
            raise SpecificationError("effective config artifact SHA-256 is invalid")
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes <= 0
        ):
            raise SpecificationError("effective config artifact size must be positive")
        if self.sidecar_root is not None:
            if not isinstance(self.sidecar_root, str) or not Path(self.sidecar_root).is_absolute():
                raise SpecificationError("effective config sidecar root must be absolute")


@dataclass(frozen=True, slots=True)
class PreparedBacktestConfiguration:
    """Explicit opt-in wrapper for an effective config generated from typed values."""

    contract: str
    config: Any
    context: Mapping[str, Any]
    effective_configuration: Mapping[str, Any]
    performance_config: Any = field(default=None, repr=False, compare=False)
    _effective_configuration_bytes: bytes = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if self.contract != EFFECTIVE_CONFIG_CONTRACT:
            raise SpecificationError("unsupported effective config contract")
        if self.config is None:
            raise SpecificationError("prepared Backtest configuration is missing")
        performance = _performance_config_projection(self.performance_config)
        context = _mapping(self.context, "effective config context", _PREPARED_CONTEXT_KEYS)
        _validate_prepared_context(context)
        effective = _mapping(
            self.effective_configuration,
            "effective configuration",
            _EFFECTIVE_KEYS,
        )
        validate_effective_configuration(effective)
        if effective["app"]["performance"] != performance:
            raise SpecificationError("effective config performance differs from typed config")
        effective_bytes = canonical_json_bytes(effective)
        object.__setattr__(self, "context", MappingProxyType(context))
        object.__setattr__(self, "effective_configuration", MappingProxyType(effective))
        object.__setattr__(self, "_effective_configuration_bytes", effective_bytes)


def canonical_json_bytes(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise SpecificationError("effective config cannot be canonically encoded") from exc


def projection_sha256(document: Mapping[str, Any]) -> str:
    body = {key: value for key, value in document.items() if key != "projection_sha256"}
    return hashlib.sha256(canonical_json_bytes(body)).hexdigest()


def render_effective_config_document(
    prepared: PreparedBacktestConfiguration,
    *,
    engine_run_id: str,
) -> bytes:
    if not isinstance(prepared, PreparedBacktestConfiguration):
        raise SpecificationError("effective config requires PreparedBacktestConfiguration")
    _nonblank(engine_run_id, "engine_run_id")
    context = {**prepared.context, "engine_run_id": engine_run_id}
    document: dict[str, Any] = {
        "schema_id": EFFECTIVE_CONFIG_SCHEMA_ID,
        "schema_version": EFFECTIVE_CONFIG_SCHEMA_VERSION,
        "context": context,
        "effective_configuration": json.loads(prepared._effective_configuration_bytes),
    }
    document["projection_sha256"] = projection_sha256(document)
    validate_effective_config_document(document)
    return canonical_json_bytes(document)


def verify_actual_backtest_configuration(prepared: PreparedBacktestConfiguration) -> None:
    """Reproject the typed config at the execution boundary and match its snapshot."""

    if not isinstance(prepared, PreparedBacktestConfiguration):
        raise SpecificationError("effective config requires PreparedBacktestConfiguration")
    try:
        from modular_backtest import BacktestAppConfig
    except ImportError as exc:
        raise SpecificationError(
            "effective config requires the official BacktestAppConfig"
        ) from exc
    if not isinstance(prepared.config, BacktestAppConfig):
        raise SpecificationError("effective config requires the official BacktestAppConfig")
    actual = _project_actual_backtest_configuration(
        prepared.config,
        expected=prepared.effective_configuration,
    )
    if canonical_json_bytes(actual) != prepared._effective_configuration_bytes:
        raise SpecificationError("effective config differs from actual Backtest configuration")


def verify_actual_performance_evidence(
    prepared: PreparedBacktestConfiguration,
    output_directory: Path,
    *,
    engine_run_id: str,
) -> None:
    """Bind the exact typed performance receipt to the Backtest producer output."""

    try:
        value = json.loads((output_directory / "application-run.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecificationError(
            "Backtest application-run performance evidence is missing"
        ) from exc
    if not isinstance(value, Mapping) or value.get("core_run_id") != engine_run_id:
        raise SpecificationError("Backtest application-run core identity differs")
    observed = value.get("performance")
    expected = prepared.effective_configuration["app"]["performance"]["values"]
    if not isinstance(observed, Mapping) or canonical_json_bytes(observed) != canonical_json_bytes(
        expected
    ):
        raise SpecificationError("Backtest producer PerformanceConfig differs from projection")


def _performance_config_projection(value: Any) -> dict[str, Any]:
    try:
        from modular_backtest.scalability import PerformanceConfig
    except ImportError as exc:
        raise SpecificationError("effective config requires Backtest PerformanceConfig") from exc
    if not isinstance(value, PerformanceConfig):
        raise SpecificationError("effective config requires a typed PerformanceConfig")
    return {
        "schema_id": PERFORMANCE_CONFIG_SCHEMA_ID,
        "schema_version": PERFORMANCE_CONFIG_SCHEMA_VERSION,
        "values": _actual_dataclass_mapping(value, "PerformanceConfig"),
    }


def official_engine_contract_profile(config: Any) -> str:
    """Return the reviewed field-capability profile for an official typed config."""

    try:
        from modular_backtest import BacktestAppConfig
    except ImportError as exc:
        raise SpecificationError(
            "effective config requires the official BacktestAppConfig"
        ) from exc
    if not isinstance(config, BacktestAppConfig):
        raise SpecificationError("effective config requires the official BacktestAppConfig")
    return _actual_engine_contract_profile(config)


def _project_actual_backtest_configuration(
    config: Any,
    *,
    expected: Mapping[str, Any],
) -> dict[str, Any]:
    _exact_dataclass_fields(config, _APP_CONFIG_FIELDS, "BacktestAppConfig")
    profile = _actual_engine_contract_profile(config)
    profile_fields = _PROFILE_FIELD_SETS[profile]
    data = config.data
    _exact_dataclass_fields(data, set(profile_fields["data_source"]), "DataSourceConfig")
    output = config.output
    _exact_dataclass_fields(output, {"directory"}, "OutputConfig")

    modules_path = config.modules
    if (
        not isinstance(modules_path, Path)
        or modules_path.is_symlink()
        or not modules_path.is_file()
    ):
        raise SpecificationError("actual module configuration path is unsafe")
    module_bytes = modules_path.read_bytes()
    try:
        rendered_module_configuration = json.loads(module_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecificationError("actual module configuration is invalid JSON") from exc
    expected_module_identity = _mapping(
        expected["module_identity"], "module_identity", _MODULE_IDENTITY_KEYS
    )
    expected_module_configuration = _mapping(
        expected_module_identity["configuration"],
        "module configuration",
        _MODULE_CONFIG_KEYS,
    )
    if canonical_json_bytes(rendered_module_configuration) != canonical_json_bytes(
        expected_module_configuration["rendered"]
    ):
        raise SpecificationError("actual module file differs from effective config identity")

    data_start = _optional_actual_timestamp(data.start, "data.start")
    data_end = _optional_actual_timestamp(data.end, "data.end")
    source_kind = (
        "registered"
        if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1 and data.storage_root is not None
        else "file"
    )
    if (
        profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
        and source_kind == "file"
        and data.registered_mode != "legacy_materialized"
    ):
        raise SpecificationError(
            "actual file source registered_mode must be legacy_materialized before normalization"
        )
    app = {
        "initial_capital": _actual_json_value(config.initial_capital),
        "requested_timeframes": _actual_json_value(config.requested_timeframes),
        "max_base_history": expected["app"]["max_base_history"],
        "max_timeframe_history": expected["app"]["max_timeframe_history"],
        "performance": expected["app"]["performance"],
        "seed": _actual_json_value(config.seed),
        "checkpoint": _actual_dataclass_mapping(config.checkpoint, "CheckpointConfig"),
        "end_of_test_policy": _actual_json_value(config.end_of_test_policy),
        "output_directory_sha256": _actual_path_sha256(output.directory),
    }
    registered_mode: str | None = None
    stream_batch_size: int | None = None
    if source_kind == "registered":
        try:
            from modular_backtest.streaming_data import resolve_registered_data_options

            registered_mode, stream_batch_size = resolve_registered_data_options(data)
        except (ImportError, TypeError, ValueError) as exc:
            raise SpecificationError("registered data options cannot be resolved") from exc
    source = {
        "source_kind": source_kind,
        "path_sha256": _actual_path_sha256(data.path),
        "storage_root_sha256": (
            _actual_path_sha256(data.storage_root)
            if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
            else None
        ),
        "symbol": _actual_json_value(data.symbol),
        "format": _actual_json_value(data.format),
        "market_type": _actual_json_value(data.market_type),
        "base_timeframe": _actual_json_value(data.base_timeframe),
        "gap_policy": _actual_json_value(data.gap_policy),
        "timestamp_column": _actual_json_value(data.timestamp_column),
        "start": data_start,
        "end": data_end,
        "columns": _actual_json_value(data.columns),
        # Dataset identity is supplied by the frozen Research Runner job when the
        # Engine uses a file source; Backtest's file DataSource has no dataset ID.
        "dataset_id": (
            expected["data_source"]["dataset_id"]
            if source_kind == "file"
            else _actual_json_value(data.dataset_id)
        ),
        "dataset_sha256": expected["data_source"]["dataset_sha256"],
        "dataset_version": (
            _actual_json_value(data.dataset_version)
            if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1
            else None
        ),
        "registered_mode": registered_mode,
        "stream_batch_size": stream_batch_size,
    }
    engine = _actual_dataclass_mapping(config.core_engine_config(), "EngineConfig")
    if "max_base_history" in _PROFILE_FIELD_SETS[profile]["engine_data"]:
        engine["data"]["max_base_history"] = app["max_base_history"]
    instrument = _actual_dataclass_mapping(config.instrument_spec(), "InstrumentSpec")
    module_identity = dict(expected_module_identity)
    module_identity["configuration_sha256"] = hashlib.sha256(module_bytes).hexdigest()
    module_identity["configuration_path_sha256"] = _actual_path_sha256(modules_path)
    actual = {
        "engine_contract_profile": profile,
        "app": app,
        "data_source": source,
        "engine": engine,
        "instrument": instrument,
        "module_identity": module_identity,
    }
    validate_effective_configuration(actual)
    return actual


def _actual_engine_contract_profile(config: Any) -> str:
    """Match only the two reviewed official typed API field signatures."""

    # Validate app values before asking the official factory method to construct
    # EngineConfig, whose own constructor may reject these values first.
    _actual_json_value(config.initial_capital)
    try:
        engine = config.core_engine_config()
        instrument = config.instrument_spec()
    except (AttributeError, TypeError, ValueError) as exc:
        raise SpecificationError(
            "official Backtest configuration cannot build typed engine values"
        ) from exc
    _exact_dataclass_fields(engine, set(_ENGINE_KEYS), "EngineConfig")
    _exact_dataclass_fields(engine.fees, set(_FEE_KEYS), "FeeConfig")
    _exact_dataclass_fields(instrument, set(_INSTRUMENT_KEYS), "InstrumentSpec")
    signatures = {
        "data_source": _dataclass_field_names(config.data, "DataSourceConfig"),
        "execution": _dataclass_field_names(engine.execution, "ExecutionConfig"),
        "risk": _dataclass_field_names(engine.risk, "RiskConfig"),
        "engine_data": _dataclass_field_names(engine.data, "DataConfig"),
    }
    matches = [
        profile
        for profile, expected in _PROFILE_FIELD_SETS.items()
        if all(signatures[key] == expected[key] for key in signatures)
    ]
    if len(matches) != 1:
        raise SpecificationError("official Backtest configuration field profile is unsupported")
    return matches[0]


def _exact_dataclass_fields(value: Any, expected: Set[str], label: str) -> None:
    actual = _dataclass_field_names(value, label)
    if actual != expected:
        raise SpecificationError(f"actual {label} fields are unsupported")


def _dataclass_field_names(value: Any, label: str) -> frozenset[str]:
    if not is_dataclass(value) or isinstance(value, type):
        raise SpecificationError(f"actual {label} must be a typed dataclass")
    return frozenset(item.name for item in fields(value))


def _actual_dataclass_mapping(value: Any, label: str) -> dict[str, Any]:
    if not is_dataclass(value) or isinstance(value, type):
        raise SpecificationError(f"actual {label} must be a typed dataclass")
    return {item.name: _actual_json_value(getattr(value, item.name)) for item in fields(value)}


def _actual_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise SpecificationError("actual Backtest config contains non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise SpecificationError("actual Backtest config contains non-finite Decimal")
        return format(value, "f")
    if isinstance(value, datetime):
        return _actual_timestamp(value)
    if is_dataclass(value) and not isinstance(value, type):
        return _actual_dataclass_mapping(value, type(value).__name__)
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise SpecificationError("actual Backtest config mapping keys must be strings")
        return {key: _actual_json_value(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_actual_json_value(item) for item in value]
    raise SpecificationError(f"unsupported actual Backtest config value: {type(value).__name__}")


def _actual_timestamp(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SpecificationError("actual Backtest timestamp must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _optional_actual_timestamp(value: Any, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise SpecificationError(f"actual {label} must be a datetime")
    return _actual_timestamp(value)


def _actual_path_sha256(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str | os.PathLike):
        raise SpecificationError("actual Backtest path must be filesystem path-like")
    return hashlib.sha256(os.fsencode(os.fspath(value))).hexdigest()


def validate_effective_config_document(value: Any) -> Mapping[str, Any]:
    document = _mapping(value, "effective config document", _DOCUMENT_KEYS)
    if document["schema_id"] != EFFECTIVE_CONFIG_SCHEMA_ID:
        raise SpecificationError("effective config schema id is unsupported")
    if document["schema_version"] != EFFECTIVE_CONFIG_SCHEMA_VERSION:
        raise SpecificationError("effective config schema version is unsupported")
    context = _mapping(document["context"], "effective config context", _CONTEXT_KEYS)
    _validate_context(context)
    configuration = _mapping(
        document["effective_configuration"], "effective configuration", _EFFECTIVE_KEYS
    )
    validate_effective_configuration(configuration)
    app = configuration["app"]
    data_source = configuration["data_source"]
    if (
        app["seed"] != context["seed"]
        or data_source["dataset_id"] != context["dataset_id"]
        or data_source["dataset_sha256"] != context["dataset_sha256"]
        or data_source["start"] != context["period_start"]
        or data_source["end"] != context["period_end"]
        or configuration["engine"]["initial_cash"] != app["initial_capital"]
        or configuration["instrument"]["symbol"] != data_source["symbol"]
        or app["performance"]["values"]["mode"] != context["execution_performance_mode"]
    ):
        raise SpecificationError("effective configuration values disagree with their context")
    digest = document["projection_sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise SpecificationError("effective config projection SHA-256 is invalid")
    if digest != projection_sha256(document):
        raise SpecificationError("effective config projection SHA-256 does not match")
    return MappingProxyType(document)


def validate_effective_configuration(value: Mapping[str, Any]) -> None:
    _validate_effective_configuration(value, schema_version=EFFECTIVE_CONFIG_SCHEMA_VERSION)


def _validate_performance_configuration(app: Mapping[str, Any], profile: str) -> Mapping[str, Any]:
    _optional_integer(app["max_base_history"], "max_base_history")
    _optional_integer(app["max_timeframe_history"], "max_timeframe_history")
    performance = _mapping(
        app["performance"], "PerformanceConfig receipt", {"schema_id", "schema_version", "values"}
    )
    if (
        performance["schema_id"] != PERFORMANCE_CONFIG_SCHEMA_ID
        or performance["schema_version"] != PERFORMANCE_CONFIG_SCHEMA_VERSION
    ):
        raise SpecificationError("PerformanceConfig receipt schema is unsupported")
    values = _mapping(performance["values"], "PerformanceConfig values", _PERFORMANCE_VALUE_KEYS)
    _enum(values["mode"], "performance.mode", {"canonical_audit", "research_fast"})
    _enum(
        values["equity_sampling"],
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
        _optional_integer(values[key], f"performance.{key}")
        if values[key] is not None and values[key] < 1:
            raise SpecificationError(f"performance.{key} must be positive")
    for key in ("progress_interval_bars", "retained_event_window", "equity_every_n_bars"):
        _integer(values[key], f"performance.{key}")
        if values[key] < 1:
            raise SpecificationError(f"performance.{key} must be positive")
    _boolean(values["disable_cyclic_gc_in_hot_loop"], "performance.disable_cyclic_gc_in_hot_loop")
    if (
        values["max_base_history"] != app["max_base_history"]
        or values["max_timeframe_history"] != app["max_timeframe_history"]
    ):
        raise SpecificationError("app history limits differ from PerformanceConfig")
    if profile == ENGINE_CONTRACT_FILE_ONLY_V1 and app["max_base_history"] is not None:
        raise SpecificationError("file-only engine contract cannot describe a base history limit")
    return values


def validate_legacy_v2_effective_config_document(value: Any) -> Mapping[str, Any]:
    """Validate the persisted v2 shape without converting it into v3 evidence."""

    document = _mapping(value, "legacy v2 effective config document", _DOCUMENT_KEYS)
    if document["schema_id"] != EFFECTIVE_CONFIG_SCHEMA_ID:
        raise SpecificationError("legacy v2 effective config schema id is unsupported")
    if document["schema_version"] != "2":
        raise SpecificationError("legacy v2 effective config schema version is unsupported")
    context = _mapping(document["context"], "legacy v2 effective config context", _CONTEXT_KEYS)
    _validate_context(context)
    configuration = _mapping(
        document["effective_configuration"],
        "legacy v2 effective configuration",
        _EFFECTIVE_KEYS,
    )
    _validate_effective_configuration(configuration, schema_version="2")
    app = configuration["app"]
    data_source = configuration["data_source"]
    if (
        app["seed"] != context["seed"]
        or data_source["dataset_id"] != context["dataset_id"]
        or data_source["dataset_sha256"] != context["dataset_sha256"]
        or data_source["start"] != context["period_start"]
        or data_source["end"] != context["period_end"]
        or configuration["engine"]["initial_cash"] != app["initial_capital"]
        or configuration["instrument"]["symbol"] != data_source["symbol"]
    ):
        raise SpecificationError("legacy v2 effective configuration disagrees with context")
    digest = document["projection_sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise SpecificationError("legacy v2 projection SHA-256 is invalid")
    if digest != projection_sha256(document):
        raise SpecificationError("legacy v2 projection SHA-256 does not match")
    return MappingProxyType(document)


def _validate_effective_configuration(value: Mapping[str, Any], *, schema_version: str) -> None:
    profile = value.get("engine_contract_profile")
    if profile not in _PROFILE_FIELD_SETS:
        raise SpecificationError("effective engine contract profile is unsupported")
    profile_fields = _PROFILE_FIELD_SETS[profile]
    app_keys = _APP_KEYS if schema_version == EFFECTIVE_CONFIG_SCHEMA_VERSION else _V2_APP_KEYS
    app = _mapping(value["app"], "effective app config", app_keys)
    _number(app["initial_capital"], "initial_capital", positive=True)
    _string_list(app["requested_timeframes"], "requested_timeframes")
    if schema_version == EFFECTIVE_CONFIG_SCHEMA_VERSION:
        _validate_performance_configuration(app, profile)
    _integer(app["seed"], "seed")
    checkpoint = _mapping(app["checkpoint"], "checkpoint", _CHECKPOINT_KEYS)
    _string(checkpoint["mode"], "checkpoint.mode")
    _optional_integer(checkpoint["restart_at_bar"], "checkpoint.restart_at_bar")
    _string(app["end_of_test_policy"], "end_of_test_policy")
    _sha256(app["output_directory_sha256"], "app.output_directory_sha256")

    source = _mapping(value["data_source"], "data_source", _SOURCE_KEYS)
    _enum(source["source_kind"], "source_kind", {"file", "registered"})
    _optional_sha256(source["path_sha256"], "data_source.path_sha256")
    _optional_sha256(source["storage_root_sha256"], "data_source.storage_root_sha256")
    if source["source_kind"] == "file" and (
        source["path_sha256"] is None or source["storage_root_sha256"] is not None
    ):
        raise SpecificationError("file data source path identity is invalid")
    if source["source_kind"] == "registered" and (
        source["path_sha256"] is not None or source["storage_root_sha256"] is None
    ):
        raise SpecificationError("registered data source path identity is invalid")
    if profile == ENGINE_CONTRACT_FILE_ONLY_V1 and source["source_kind"] != "file":
        raise SpecificationError("file-only engine contract cannot describe registered data")
    for key in ("symbol", "market_type", "base_timeframe", "gap_policy"):
        _string(source[key], f"data_source.{key}")
    _nonblank(source["timestamp_column"], "data_source.timestamp_column")
    _optional_string(source["format"], "data_source.format")
    _optional_string(source["dataset_id"], "data_source.dataset_id")
    _optional_sha256(source["dataset_sha256"], "data_source.dataset_sha256")
    _optional_integer(source["dataset_version"], "data_source.dataset_version")
    _optional_string(source["registered_mode"], "data_source.registered_mode")
    _optional_integer(source["stream_batch_size"], "data_source.stream_batch_size")
    _optional_timestamp(source["start"], "data_source.start")
    _optional_timestamp(source["end"], "data_source.end")
    columns = _mapping(source["columns"], "data_source.columns", _COLUMN_KEYS)
    for key, column in columns.items():
        _nonblank(column, f"data_source.columns.{key}")
    if schema_version == EFFECTIVE_CONFIG_SCHEMA_VERSION and (
        source["source_kind"] == "registered"
        and source["registered_mode"] == "legacy_materialized"
        and checkpoint["mode"] == "replay"
        and app["performance"]["values"]["mode"] == "canonical_audit"
    ):
        raise SpecificationError(
            "effective config cannot attest canonical-audit replay for registered "
            "legacy-materialized data"
        )

    engine = _mapping(value["engine"], "engine config", profile_fields["engine"])
    _number(engine["initial_cash"], "engine.initial_cash", positive=True)
    for key in (
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
        _boolean(engine[key], f"engine.{key}")
    fees = _mapping(engine["fees"], "engine.fees", _FEE_KEYS)
    for key, item in fees.items():
        _number(item, f"engine.fees.{key}")
    execution = _mapping(engine["execution"], "engine.execution", profile_fields["execution"])
    for key in (
        "intrabar_priority",
        "limit_fill_policy",
        "target_liquidity",
        "stop_limit_same_bar_policy",
    ):
        _string(execution[key], f"engine.execution.{key}")
    if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _string(execution["close_request_timing"], "engine.execution.close_request_timing")
    for key in (
        "allow_exit_on_entry_bar",
        "allow_reentry_on_exit_bar",
    ):
        _boolean(execution[key], f"engine.execution.{key}")
    if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _boolean(
            execution["allow_limit_stop_on_entry_bar"],
            "engine.execution.allow_limit_stop_on_entry_bar",
        )
    for key in ("slippage_bps", "stop_slippage_bps", "max_fill_fraction_per_bar"):
        _number(execution[key], f"engine.execution.{key}")
    _optional_number(
        execution["max_fill_quantity_per_bar"], "engine.execution.max_fill_quantity_per_bar"
    )
    risk = _mapping(engine["risk"], "engine.risk", profile_fields["risk"])
    if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _string(risk["sizing_model"], "engine.risk.sizing_model")
    for key in (
        "default_risk_fraction",
        "max_risk_fraction",
        "max_notional_multiple",
        "min_notional",
        "min_quantity",
        "quantity_step",
        "min_stop_distance_pct",
    ):
        _number(risk[key], f"engine.risk.{key}")
    _optional_number(risk["max_stop_distance_pct"], "engine.risk.max_stop_distance_pct")
    data = _mapping(engine["data"], "engine.data", profile_fields["engine_data"])
    _optional_integer(data["expected_interval_seconds"], "engine.data.expected_interval_seconds")
    _string(data["gap_policy"], "engine.data.gap_policy")
    _string(data["timestamp_semantics"], "engine.data.timestamp_semantics")
    if profile == ENGINE_CONTRACT_EXTENDED_REGISTERED_V1:
        _optional_integer(data["max_base_history"], "engine.data.max_base_history")

    instrument = _mapping(value["instrument"], "instrument", _INSTRUMENT_KEYS)
    for key in ("symbol", "base_asset", "quote_asset", "market_type"):
        _nonblank(instrument[key], f"instrument.{key}")
    for key in ("tick_size", "quantity_step", "min_quantity", "min_notional", "contract_size"):
        _number(instrument[key], f"instrument.{key}")
    for key in ("max_quantity", "max_notional"):
        _optional_number(instrument[key], f"instrument.{key}")
    for key in ("price_precision", "quantity_precision"):
        _optional_integer(instrument[key], f"instrument.{key}")

    module_identity = _mapping(value["module_identity"], "module_identity", _MODULE_IDENTITY_KEYS)
    digest = module_identity["configuration_sha256"]
    if not isinstance(digest, str) or _SHA256.fullmatch(digest) is None:
        raise SpecificationError("module configuration SHA-256 is invalid")
    _sha256(
        module_identity["configuration_path_sha256"],
        "module_identity.configuration_path_sha256",
    )
    module_config = _mapping(
        module_identity["configuration"], "module configuration", _MODULE_CONFIG_KEYS
    )
    modules = module_config["modules"]
    if not isinstance(modules, list) or not modules:
        raise SpecificationError("module configuration modules must be a non-empty list")
    for index, raw in enumerate(modules):
        module = _mapping(raw, f"module configuration modules[{index}]", _MODULE_KEYS)
        for key in (
            "core_manifest_name",
            "runtime_component_id",
            "runtime_component_version",
            "import_path",
            "api_version",
            "state_schema_version",
        ):
            _string(module[key], f"modules[{index}].{key}")
        source_revision = module["source_commit_sha"]
        if (
            not isinstance(source_revision, str)
            or _SOURCE_REVISION.fullmatch(source_revision) is None
        ):
            raise SpecificationError(f"modules[{index}].source_commit_sha is invalid")
        _mapping(module["parameter_schema"], f"modules[{index}].parameter_schema")
        _mapping(module["parameters"], f"modules[{index}].parameters")
    core_manifest_names = [module["core_manifest_name"] for module in modules]
    if len(set(core_manifest_names)) != len(core_manifest_names):
        raise SpecificationError("Core module manifest names must be unique")
    rendered = _mapping(module_config["rendered"], "rendered module configuration", {"modules"})
    rendered_modules = rendered["modules"]
    if not isinstance(rendered_modules, list) or len(rendered_modules) != len(modules):
        raise SpecificationError("rendered module configuration does not match identities")
    for index, (identity, rendered_item) in enumerate(zip(modules, rendered_modules, strict=True)):
        rendered_identity = _mapping(
            rendered_item,
            f"rendered module configuration modules[{index}]",
            frozenset({"class", "enabled", "parameters"}),
        )
        if (
            rendered_identity["class"] != identity["import_path"]
            or rendered_identity["enabled"] is not True
            or canonical_json_bytes(rendered_identity["parameters"])
            != canonical_json_bytes(identity["parameters"])
        ):
            raise SpecificationError("rendered module configuration identity does not match")
    rendered_digest = hashlib.sha256(canonical_json_bytes(rendered)).hexdigest()
    if digest != rendered_digest:
        raise SpecificationError("module configuration digest does not match rendered values")
    components = module_identity["components"]
    if not isinstance(components, list) or not components:
        raise SpecificationError("module identities must be a non-empty list")
    _unique_string_values(components, "module identities")
    if components != [module["runtime_component_id"] for module in modules]:
        raise SpecificationError("module identity component list does not match configuration")


def sidecar_path_for_job(job_output_directory: Path) -> Path:
    if not isinstance(job_output_directory, Path) or not job_output_directory.is_absolute():
        raise SpecificationError("job output directory must be an absolute path")
    if job_output_directory.is_symlink() or not job_output_directory.is_dir():
        raise SpecificationError("job output directory must be a real directory")
    return (
        job_output_directory.parent
        / f".{job_output_directory.name}.runner-artifacts"
        / EFFECTIVE_CONFIG_FILENAME
    )


def write_sidecar_for_job(
    job_output_directory: Path,
    prepared: PreparedBacktestConfiguration,
    *,
    engine_run_id: str,
    result_output_directory: Path | None = None,
) -> EffectiveConfigArtifactReference:
    sidecar_root: str | None = None
    if result_output_directory is not None:
        try:
            job_root = job_output_directory.resolve(strict=True)
            result_root = result_output_directory.resolve(strict=True)
            result_root.relative_to(job_root)
        except (OSError, ValueError) as exc:
            raise SpecificationError(
                "result output must be inside the effective config job root"
            ) from exc
        sidecar_root = str(job_root)
    payload = render_effective_config_document(prepared, engine_run_id=engine_run_id)
    path = sidecar_path_for_job(job_output_directory)
    if path.parent.is_symlink():
        raise SpecificationError("effective config artifact directory cannot be a symlink")
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise SpecificationError("effective config artifact cannot be a symlink")
    if path.exists():
        if not path.is_file() or path.read_bytes() != payload:
            raise SpecificationError("existing effective config artifact differs")
    else:
        descriptor, temporary_name = tempfile.mkstemp(prefix=".effective-config-", dir=path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
    return EffectiveConfigArtifactReference(
        relative_path=EFFECTIVE_CONFIG_FILENAME,
        sha256=hashlib.sha256(payload).hexdigest(),
        size_bytes=len(payload),
        sidecar_root=sidecar_root,
    )


def read_sidecar_for_job(
    job_output_directory: Path,
    reference: EffectiveConfigArtifactReference,
) -> tuple[bytes, Mapping[str, Any]]:
    payload, value = _read_sidecar_payload(job_output_directory, reference)
    document = validate_effective_config_document(value)
    return payload, document


def read_legacy_v2_sidecar_for_job(
    job_output_directory: Path,
    reference: EffectiveConfigArtifactReference,
) -> tuple[bytes, Mapping[str, Any]]:
    if reference.sidecar_root is not None:
        raise SpecificationError("legacy v2 sidecar cannot declare a v3 sidecar root")
    payload, value = _read_sidecar_payload(job_output_directory, reference)
    if canonical_json_bytes(value) != payload:
        raise SpecificationError("legacy v2 sidecar is not canonical JSON")
    document = validate_legacy_v2_effective_config_document(value)
    return payload, document


def _read_sidecar_payload(
    job_output_directory: Path,
    reference: EffectiveConfigArtifactReference,
) -> tuple[bytes, Any]:
    if not isinstance(reference, EffectiveConfigArtifactReference):
        raise SpecificationError("effective config artifact reference is invalid")
    path = sidecar_path_for_job(job_output_directory)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise SpecificationError("effective config artifact directory is unsafe")
    try:
        info = path.lstat()
    except OSError as exc:
        raise SpecificationError("effective config artifact is missing") from exc
    if not stat.S_ISREG(info.st_mode) or info.st_size != reference.size_bytes:
        raise SpecificationError("effective config artifact size or type changed")
    payload = path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != reference.sha256:
        raise SpecificationError("effective config artifact hash changed")
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SpecificationError("effective config artifact is invalid JSON") from exc
    return payload, value


def _validate_context(value: Mapping[str, Any]) -> None:
    for key in (
        "run_id",
        "engine_run_id",
        "experiment_id",
        "stage_id",
        "stage_role",
        "split_id",
        "dataset_id",
        "scenario_id",
    ):
        _nonblank(value[key], key)
    for key in ("dataset_sha256", "configuration_sha256"):
        _sha256(value[key], key)
    _integer(value["seed"], "seed")
    _integer(value["repetition_index"], "repetition_index")
    if value["repetition_index"] < 0:
        raise SpecificationError("repetition_index must be a non-negative integer")
    _timestamp(value["period_start"], "period_start")
    _timestamp(value["period_end"], "period_end")
    if value["execution_performance_mode"] not in ("canonical_audit", "research_fast"):
        raise SpecificationError("effective config execution performance mode is invalid")


def _validate_prepared_context(value: Mapping[str, Any]) -> None:
    for key in (
        "run_id",
        "experiment_id",
        "stage_id",
        "stage_role",
        "split_id",
        "dataset_id",
        "scenario_id",
    ):
        _nonblank(value[key], key)
    for key in ("dataset_sha256", "configuration_sha256"):
        _sha256(value[key], key)
    _integer(value["seed"], "seed")
    _integer(value["repetition_index"], "repetition_index")
    if value["repetition_index"] < 0:
        raise SpecificationError("repetition_index must be a non-negative integer")
    _timestamp(value["period_start"], "period_start")
    _timestamp(value["period_end"], "period_end")
    if value["execution_performance_mode"] not in ("canonical_audit", "research_fast"):
        raise SpecificationError("effective config execution performance mode is invalid")


def _mapping(
    value: Any,
    label: str,
    keys: frozenset[str] | set[str] | None = None,
) -> dict[str, Any]:
    if (
        not isinstance(value, Mapping)
        or (keys is not None and set(value) != keys)
        or any(not isinstance(key, str) for key in value)
    ):
        raise SpecificationError(f"{label} fields are invalid")
    return dict(value)


def _nonblank(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value or value != value.strip():
        raise SpecificationError(f"{label} must be a canonical non-blank string")


def _string(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value:
        raise SpecificationError(f"{label} must be a non-empty string")


def _optional_string(value: Any, label: str) -> None:
    if value is not None:
        _string(value, label)


def _boolean(value: Any, label: str) -> None:
    if not isinstance(value, bool):
        raise SpecificationError(f"{label} must be boolean")


def _integer(value: Any, label: str) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise SpecificationError(f"{label} must be an integer")


def _optional_integer(value: Any, label: str) -> None:
    if value is not None:
        _integer(value, label)


def _number(value: Any, label: str, *, positive: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int | float)
        or not math.isfinite(float(value))
    ):
        raise SpecificationError(f"{label} must be a finite number")
    if positive and value <= 0:
        raise SpecificationError(f"{label} must be positive")


def _optional_number(value: Any, label: str) -> None:
    if value is not None:
        _number(value, label)


def _string_list(value: Any, label: str) -> None:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise SpecificationError(f"{label} must be a list of non-empty strings")


def _enum(value: Any, label: str, allowed: set[str]) -> None:
    if not isinstance(value, str) or value not in allowed:
        raise SpecificationError(f"{label} is unsupported")


def _timestamp(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise SpecificationError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as exc:
        raise SpecificationError(f"{label} must be a canonical UTC timestamp") from exc
    if parsed.utcoffset() != UTC.utcoffset(parsed):
        raise SpecificationError(f"{label} must be a canonical UTC timestamp")
    if parsed.isoformat(timespec="microseconds").replace("+00:00", "Z") != value:
        raise SpecificationError(f"{label} must be a canonical UTC timestamp")


def _optional_timestamp(value: Any, label: str) -> None:
    if value is not None:
        _timestamp(value, label)


def _sha256(value: Any, label: str) -> None:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise SpecificationError(f"{label} must be a lowercase SHA-256")


def _optional_sha256(value: Any, label: str) -> None:
    if value is not None:
        _sha256(value, label)


def _unique_string_values(value: Sequence[Any], label: str) -> None:
    if any(not isinstance(item, str) or not item for item in value) or len(set(value)) != len(
        value
    ):
        raise SpecificationError(f"{label} must contain unique non-empty strings")
