"""Trusted construction of official Backtest Engine application configurations."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields, is_dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType
from typing import Any
from urllib.parse import unquote, urlparse

from modular_research_runner.effective_config import (
    EFFECTIVE_CONFIG_CONTRACT,
    PreparedBacktestConfiguration,
    canonical_json_bytes,
    official_engine_contract_profile,
)

from .contracts import MarketType, StrategyFactoryRequest
from .errors import CompatibilityError

_DEFAULT_COLUMNS = MappingProxyType(
    {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
)
_PROVENANCE_FIELDS = frozenset(
    {
        "architecture_id",
        "architecture_fingerprint",
        "modules",
        "runtime_modules",
        "parameters",
        "factory_run_id",
        "factory_manifest_id",
        "factory_candidate_hash",
    }
)
_REQUIRED_EXECUTION_EVIDENCE = frozenset({"slippage_bps", "stop_slippage_bps"})
_REQUIRED_FEE_EVIDENCE = frozenset({"maker_rate", "taker_rate"})
_REQUIRED_INSTRUMENT_EVIDENCE = frozenset(
    {
        "base_asset",
        "quote_asset",
        "tick_size",
        "quantity_step",
        "min_quantity",
        "min_notional",
        "contract_size",
    }
)


@dataclass(frozen=True, slots=True)
class TrustedRuntimeModule:
    """Code-owned allowlist record for one executable runtime component."""

    runtime_component_id: str
    runtime_component_version: str
    import_path: str
    source_commit_sha: str

    def __post_init__(self) -> None:
        for name in (
            "runtime_component_id",
            "runtime_component_version",
            "import_path",
            "source_commit_sha",
        ):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise CompatibilityError(f"{name} must be a canonical non-blank string")
        if ":" not in self.import_path and "." not in self.import_path:
            raise CompatibilityError("trusted import_path is not importable")
        if len(self.source_commit_sha) not in {40, 64} or any(
            character not in "0123456789abcdef" for character in self.source_commit_sha
        ):
            raise CompatibilityError("trusted source_commit_sha must be lowercase hex")

    @property
    def key(self) -> tuple[str, str]:
        return self.runtime_component_id, self.runtime_component_version


@dataclass(frozen=True, slots=True)
class BacktestEnginePolicy:
    """Explicit execution assumptions required by the official Backtest Engine."""

    execution: Mapping[str, Any]
    fees: Mapping[str, Any]
    instrument: Mapping[str, Any]
    risk: Mapping[str, Any] = field(default_factory=dict)
    gap_policy: str = "reject"
    timestamp_column: str = "open_time"
    columns: Mapping[str, str] = field(default_factory=lambda: _DEFAULT_COLUMNS)
    checkpoint_mode: str = "disabled"
    restart_at_bar: int | None = None
    end_of_test_policy: str = "force_close"

    def __post_init__(self) -> None:
        for name in ("execution", "fees", "instrument", "risk", "columns"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise CompatibilityError(f"backtest policy {name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        missing_execution = sorted(_REQUIRED_EXECUTION_EVIDENCE - set(self.execution))
        if missing_execution:
            raise CompatibilityError(
                "backtest execution evidence is incomplete: " + ", ".join(missing_execution)
            )
        missing_fees = sorted(_REQUIRED_FEE_EVIDENCE - set(self.fees))
        if missing_fees:
            raise CompatibilityError(
                "backtest fee evidence is incomplete: " + ", ".join(missing_fees)
            )
        missing_instrument = sorted(_REQUIRED_INSTRUMENT_EVIDENCE - set(self.instrument))
        if missing_instrument:
            raise CompatibilityError(
                "backtest instrument evidence is incomplete: " + ", ".join(missing_instrument)
            )
        if self.gap_policy not in {"allow", "reject"}:
            raise CompatibilityError("gap_policy must be allow or reject")
        if not isinstance(self.timestamp_column, str) or not self.timestamp_column.strip():
            raise CompatibilityError("timestamp_column must be non-blank")
        if set(self.columns) != {"open", "high", "low", "close", "volume"}:
            raise CompatibilityError(
                "columns must define exactly open, high, low, close and volume"
            )
        physical = (self.timestamp_column, *self.columns.values())
        if any(not isinstance(item, str) or not item.strip() for item in physical):
            raise CompatibilityError("data columns must be non-blank strings")
        if len(set(physical)) != len(physical):
            raise CompatibilityError("timestamp and OHLCV columns must be distinct")
        if self.checkpoint_mode not in {"disabled", "replay"}:
            raise CompatibilityError("checkpoint_mode must be disabled or replay")
        if self.restart_at_bar is not None and (
            isinstance(self.restart_at_bar, bool)
            or not isinstance(self.restart_at_bar, int)
            or self.restart_at_bar < 0
        ):
            raise CompatibilityError("restart_at_bar must be a non-negative int or None")
        if self.restart_at_bar is not None and self.checkpoint_mode != "replay":
            raise CompatibilityError("restart_at_bar requires replay checkpoint mode")
        if self.end_of_test_policy not in {"force_close", "leave_open"}:
            raise CompatibilityError("end_of_test_policy must be force_close or leave_open")
        self._validate_core_assumptions()

    def _validate_core_assumptions(self) -> None:
        try:
            from modular_core import ExecutionConfig, FeeConfig, InstrumentSpec, RiskConfig
        except ImportError as exc:  # pragma: no cover - environment contract
            raise CompatibilityError("modular_core is unavailable") from exc
        try:
            ExecutionConfig(**dict(self.execution))
            FeeConfig(**dict(self.fees))
            RiskConfig(**dict(self.risk))
            InstrumentSpec(symbol="VALIDATIONUSDT", **dict(self.instrument))
        except (TypeError, ValueError) as exc:
            raise CompatibilityError(f"backtest execution assumptions are invalid: {exc}") from exc


class OfficialBacktestConfigFactory:
    """Build official ``modular_backtest.BacktestAppConfig`` from a frozen job.

    The job can select values only inside the already-registered parameter schema.
    Import paths and source revisions are accepted exclusively when they match the
    constructor's code-owned trusted-module allowlist.
    """

    def __init__(
        self,
        *,
        request: StrategyFactoryRequest,
        policy: BacktestEnginePolicy,
        trusted_modules: Sequence[TrustedRuntimeModule],
        allowed_output_root: str | Path,
        preflight_modules: bool = True,
        effective_config_contract: str | None = None,
        execution_performance_mode: str | None = None,
    ) -> None:
        if not isinstance(request, StrategyFactoryRequest):
            raise TypeError("request must be StrategyFactoryRequest")
        if not isinstance(policy, BacktestEnginePolicy):
            raise TypeError("policy must be BacktestEnginePolicy")
        if isinstance(trusted_modules, (str, bytes)) or not isinstance(trusted_modules, Sequence):
            raise TypeError("trusted_modules must be a sequence")
        normalized = tuple(trusted_modules)
        if not normalized or any(not isinstance(item, TrustedRuntimeModule) for item in normalized):
            raise CompatibilityError("trusted_modules must contain TrustedRuntimeModule records")
        by_key = {item.key: item for item in normalized}
        if len(by_key) != len(normalized):
            raise CompatibilityError("trusted module identities must be unique")
        if not isinstance(preflight_modules, bool):
            raise TypeError("preflight_modules must be bool")
        if effective_config_contract not in {None, EFFECTIVE_CONFIG_CONTRACT}:
            raise CompatibilityError("effective_config_contract is unsupported")
        if execution_performance_mode not in {None, "canonical_audit", "research_fast"}:
            raise CompatibilityError("execution_performance_mode is unsupported")
        if effective_config_contract is not None and execution_performance_mode is None:
            raise CompatibilityError("effective config requires an explicit performance mode")
        raw_output_root = Path(allowed_output_root).expanduser()
        if raw_output_root.exists() and raw_output_root.is_symlink():
            raise CompatibilityError("allowed_output_root cannot be a symlink")
        raw_output_root.mkdir(parents=True, exist_ok=True)
        output_root = raw_output_root.resolve()
        if not output_root.is_dir():
            raise CompatibilityError("allowed_output_root must be a real directory")
        self._request = request
        self._policy = policy
        self._trusted = MappingProxyType(by_key)
        self._output_root = output_root
        self._preflight_modules = preflight_modules
        self._effective_config_contract = effective_config_contract
        self._execution_performance_mode = execution_performance_mode

    def __call__(self, job: Any) -> Any:
        try:
            from modular_backtest import BacktestAppConfig, CheckpointConfig
            from modular_backtest.config import DataSourceConfig, OutputConfig
        except ImportError as exc:  # pragma: no cover - environment contract
            raise CompatibilityError("official modular_backtest is unavailable") from exc

        parameters = getattr(job, "parameters", None)
        if not isinstance(parameters, Mapping):
            raise CompatibilityError("Research Runner job parameters must be a mapping")
        output_directory = self._job_output_directory(job)
        dataset_path = _local_dataset_path(self._request.dataset.uri)
        _verify_regular_file_hash(dataset_path, self._request.dataset.sha256)
        start, end = self._job_period(job)
        modules_payload = self._module_configuration(parameters)
        modules_path = self._write_modules_configuration(output_directory, modules_payload)
        requested_timeframes = tuple(
            timeframe
            for timeframe in self._request.allowed_timeframes
            if timeframe != self._request.dataset.base_timeframe
        )
        unsupported = sorted(set(requested_timeframes) - {"15m", "1h", "4h", "1d"})
        if unsupported:
            raise CompatibilityError(
                "Backtest Engine does not support requested timeframes: " + ", ".join(unsupported)
            )
        risk = dict(self._policy.risk)
        research_risk = float(self._request.research_risk_fraction)
        configured_default = risk.setdefault("default_risk_fraction", research_risk)
        if Decimal(str(configured_default)) != self._request.research_risk_fraction:
            raise CompatibilityError(
                "Backtest risk default differs from Strategy Factory research risk"
            )
        risk.setdefault("max_risk_fraction", max(research_risk, research_risk))
        data = DataSourceConfig(
            path=dataset_path,
            format=self._request.dataset.file_format,
            symbol=self._request.symbol,
            market_type=_backtest_market_type(self._request),
            base_timeframe=self._request.dataset.base_timeframe,
            gap_policy=self._policy.gap_policy,
            timestamp_column=self._policy.timestamp_column,
            start=start,
            end=end,
            columns=self._policy.columns,
        )
        config = BacktestAppConfig(
            data=data,
            modules=modules_path,
            initial_capital=float(self._request.initial_capital),
            requested_timeframes=requested_timeframes,
            execution=self._policy.execution,
            fees=self._policy.fees,
            risk=risk,
            instrument=self._policy.instrument,
            output=OutputConfig(output_directory / "engine-results"),
            seed=_job_seed(job),
            checkpoint=CheckpointConfig(
                mode=self._policy.checkpoint_mode,
                restart_at_bar=self._policy.restart_at_bar,
            ),
            end_of_test_policy=self._policy.end_of_test_policy,
        )
        # Force validation at the Factory/Engine boundary, not after worker dispatch.
        config.core_engine_config()
        config.instrument_spec()
        if self._preflight_modules:
            from modular_core import ModuleRegistry

            modules = ModuleRegistry().load_config(modules_path)
            ModuleRegistry().validate_and_plan(
                modules, base_timeframe=self._request.dataset.base_timeframe
            )
        if self._effective_config_contract is None:
            return config
        return self._prepared_effective_configuration(
            job,
            config,
            modules_payload=modules_payload,
            modules_path=modules_path,
        )

    def _prepared_effective_configuration(
        self,
        job: Any,
        config: Any,
        *,
        modules_payload: Mapping[str, Any],
        modules_path: Path,
        performance_config: Any | None = None,
    ) -> PreparedBacktestConfiguration:
        """Bind the actual typed app/Core/instrument values without filesystem paths."""

        if self._effective_config_contract != EFFECTIVE_CONFIG_CONTRACT:
            raise CompatibilityError("effective config contract was not explicitly enabled")
        if self._execution_performance_mode is None:
            raise CompatibilityError("effective config requires an explicit performance mode")
        try:
            from modular_backtest.scalability import PerformanceConfig
        except ImportError as exc:
            raise CompatibilityError(
                "effective config requires Backtest PerformanceConfig"
            ) from exc
        if performance_config is None:
            performance_config = PerformanceConfig(mode=self._execution_performance_mode)
        if not isinstance(performance_config, PerformanceConfig):
            raise CompatibilityError("effective config requires typed PerformanceConfig")
        if performance_config.mode != self._execution_performance_mode:
            raise CompatibilityError("PerformanceConfig mode differs from execution mode")
        engine_config = config.core_engine_config()
        engine_config = replace(
            engine_config,
            data=replace(
                engine_config.data,
                max_base_history=performance_config.max_base_history,
            ),
        )
        instrument_spec = config.instrument_spec()
        data = config.data
        start = _effective_timestamp(data.start, "data.start")
        end = _effective_timestamp(data.end, "data.end")
        metadata = getattr(job, "metadata", None)
        if not isinstance(metadata, Mapping):
            raise CompatibilityError("Research Runner job metadata must be a mapping")
        configuration_sha256 = metadata.get("configuration_sha256")
        if (
            not isinstance(configuration_sha256, str)
            or len(configuration_sha256) != 64
            or any(character not in "0123456789abcdef" for character in configuration_sha256)
        ):
            raise CompatibilityError("Research Runner job configuration hash is invalid")
        if config.seed != _job_seed(job):
            raise CompatibilityError("effective config seed differs from Research Runner job")

        raw_modules = getattr(job, "parameters", {}).get("runtime_modules")
        if not isinstance(raw_modules, (tuple, list)):
            raise CompatibilityError("runtime module blueprint is missing")
        rendered_modules = modules_payload.get("modules")
        if not isinstance(rendered_modules, (tuple, list)) or len(rendered_modules) != len(
            raw_modules
        ):
            raise CompatibilityError("rendered module configuration differs from blueprint")
        from modular_core import ModuleRegistry
        from modular_core.manifest import manifest_for, parameters_for

        module_registry = ModuleRegistry()
        loaded_modules = module_registry.load_config(modules_path)
        module_registry.validate_and_plan(
            loaded_modules, base_timeframe=self._request.dataset.base_timeframe
        )
        if len(loaded_modules) != len(raw_modules):
            raise CompatibilityError("loaded Core modules differ from effective module blueprint")
        module_identities: list[dict[str, Any]] = []
        for index, (raw_module, rendered_module, loaded_module) in enumerate(
            zip(raw_modules, rendered_modules, loaded_modules, strict=True)
        ):
            if not isinstance(raw_module, Mapping) or not isinstance(rendered_module, Mapping):
                raise CompatibilityError(f"runtime module {index} is malformed")
            manifest = manifest_for(loaded_module)
            manifest_name = manifest.name
            loaded_name = getattr(loaded_module, "name", None)
            loaded_parameters = parameters_for(loaded_module)
            rendered_parameters = rendered_module.get("parameters")
            blueprint_schema = raw_module.get("parameter_schema")
            if (
                not isinstance(manifest_name, str)
                or not manifest_name
                or loaded_name != manifest_name
                or not isinstance(loaded_parameters, Mapping)
                or not isinstance(rendered_parameters, Mapping)
                or not isinstance(blueprint_schema, Mapping)
                or canonical_json_bytes(_json_primitive(blueprint_schema))
                != canonical_json_bytes(_json_primitive(manifest.parameters_schema))
                or canonical_json_bytes(_json_primitive(loaded_parameters))
                != canonical_json_bytes(_json_primitive(rendered_parameters))
            ):
                raise CompatibilityError(
                    f"loaded Core module {index} differs from effective module configuration"
                )
            expected_identity = {
                key: raw_module.get(key)
                for key in (
                    "runtime_component_id",
                    "runtime_component_version",
                    "import_path",
                    "api_version",
                    "state_schema_version",
                    "source_commit_sha",
                    "parameter_schema",
                )
            }
            expected_identity["core_manifest_name"] = manifest_name
            expected_identity["parameters"] = rendered_module.get("parameters")
            module_identities.append(_json_primitive(expected_identity))

        module_bytes = modules_path.read_bytes()
        if data.is_registered:
            from modular_backtest.streaming_data import resolve_registered_data_options

            registered_mode, stream_batch_size = resolve_registered_data_options(data)
        else:
            registered_mode, stream_batch_size = None, None
        configuration: dict[str, Any] = {
            "engine_contract_profile": official_engine_contract_profile(config),
            "app": {
                "initial_capital": config.initial_capital,
                "requested_timeframes": list(config.requested_timeframes),
                "max_base_history": getattr(engine_config.data, "max_base_history", None),
                "max_timeframe_history": performance_config.max_timeframe_history,
                "performance": {
                    "schema_id": "modular-backtest.performance-config",
                    "schema_version": "1",
                    "values": {
                        item.name: getattr(performance_config, item.name)
                        for item in fields(performance_config)
                    },
                },
                "seed": config.seed,
                "checkpoint": _typed_dataclass_mapping(config.checkpoint, "checkpoint"),
                "end_of_test_policy": config.end_of_test_policy,
                "output_directory_sha256": _path_identity_sha256(config.output.directory),
            },
            "data_source": {
                "source_kind": "registered" if data.is_registered else "file",
                "path_sha256": _path_identity_sha256(data.path),
                "storage_root_sha256": _path_identity_sha256(data.storage_root),
                "symbol": data.symbol,
                "format": data.format,
                "market_type": data.market_type,
                "base_timeframe": data.base_timeframe,
                "gap_policy": data.gap_policy,
                "timestamp_column": data.timestamp_column,
                "start": start,
                "end": end,
                "columns": _json_primitive(data.columns),
                "dataset_id": self._request.dataset.dataset_id,
                "dataset_sha256": self._request.dataset.sha256,
                "dataset_version": data.dataset_version,
                "registered_mode": registered_mode,
                "stream_batch_size": stream_batch_size,
            },
            "engine": _typed_dataclass_mapping(engine_config, "engine"),
            "instrument": _typed_dataclass_mapping(instrument_spec, "instrument"),
            "module_identity": {
                "configuration_sha256": hashlib.sha256(module_bytes).hexdigest(),
                "configuration_path_sha256": _path_identity_sha256(modules_path),
                "configuration": _json_primitive(modules_payload),
                "components": [item["runtime_component_id"] for item in module_identities],
            },
        }
        context = {
            "run_id": _job_text(job, "run_id"),
            "experiment_id": _job_text(job, "experiment_id"),
            "stage_id": _job_text(job, "stage_id"),
            "stage_role": _job_text(job, "stage_role"),
            "split_id": _job_text(job, "split_id"),
            "dataset_id": _job_text(job, "dataset_id"),
            "dataset_sha256": self._request.dataset.sha256,
            "scenario_id": _job_text(job, "scenario_id"),
            "seed": _job_seed(job),
            "repetition_index": _job_int(job, "repetition_index"),
            "period_start": start,
            "period_end": end,
            "configuration_sha256": configuration_sha256,
            "execution_performance_mode": self._execution_performance_mode,
        }
        configuration["module_identity"]["components"] = [
            item["runtime_component_id"] for item in module_identities
        ]
        configuration["module_identity"]["configuration"] = {
            "modules": module_identities,
            "rendered": _json_primitive(modules_payload),
        }
        return PreparedBacktestConfiguration(
            contract=EFFECTIVE_CONFIG_CONTRACT,
            config=config,
            context=context,
            effective_configuration=configuration,
            performance_config=performance_config,
        )

    def _job_output_directory(self, job: Any) -> Path:
        raw = getattr(job, "output_directory", None)
        if not isinstance(raw, Path):
            raise CompatibilityError("Research Runner job output_directory must be Path")
        expanded = raw.expanduser()
        if not expanded.is_absolute():
            raise CompatibilityError("Research Runner job output_directory must be absolute")
        lexical = Path(os.path.abspath(expanded))
        if lexical != self._output_root and self._output_root not in lexical.parents:
            raise CompatibilityError("job output_directory escapes allowed_output_root")
        _reject_symlink_chain(lexical, stop=self._output_root)
        candidate = lexical.resolve()
        if candidate != self._output_root and self._output_root not in candidate.parents:
            raise CompatibilityError("job output_directory resolves outside allowed_output_root")
        candidate.mkdir(parents=True, exist_ok=True)
        return candidate

    def _job_period(self, job: Any) -> tuple[datetime, datetime]:
        period = getattr(job, "period", None)
        if period is None:
            start = self._request.dataset.period.start
            end = self._request.dataset.period.end
        else:
            start = _utc_datetime(getattr(period, "start", None), "job.period.start")
            end = _utc_datetime(getattr(period, "end", None), "job.period.end")
        dataset_period = self._request.dataset.period
        if start < dataset_period.start or end > dataset_period.end or end <= start:
            raise CompatibilityError("Research Runner job period escapes dataset bounds")
        return start, end

    def _module_configuration(self, parameters: Mapping[str, Any]) -> Mapping[str, Any]:
        raw_blueprint = parameters.get("runtime_modules")
        if not isinstance(raw_blueprint, (tuple, list)) or not raw_blueprint:
            raise CompatibilityError("Research Runner job has no runtime_modules blueprint")
        rows: list[dict[str, Any]] = []
        component_ids: set[str] = set()
        component_order: list[str] = []
        schemas: dict[str, Mapping[str, Any]] = {}
        fixed_by_component: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(raw_blueprint):
            if not isinstance(raw, Mapping):
                raise CompatibilityError(f"runtime_modules[{index}] must be a mapping")
            allowed = {
                "runtime_component_id",
                "runtime_component_version",
                "import_path",
                "fixed_parameters",
                "parameter_schema",
                "api_version",
                "state_schema_version",
                "source_commit_sha",
            }
            unknown = sorted(set(raw) - allowed)
            if unknown:
                raise CompatibilityError(
                    f"runtime_modules[{index}] has unknown fields: " + ", ".join(unknown)
                )
            component_id = _text(
                raw.get("runtime_component_id"),
                f"runtime_modules[{index}].runtime_component_id",
            )
            version = _text(
                raw.get("runtime_component_version"),
                f"runtime_modules[{index}].runtime_component_version",
            )
            import_path = _text(raw.get("import_path"), f"runtime_modules[{index}].import_path")
            source_commit_sha = _text(
                raw.get("source_commit_sha"),
                f"runtime_modules[{index}].source_commit_sha",
            )
            trusted = self._trusted.get((component_id, version))
            if trusted is None:
                raise CompatibilityError(
                    f"runtime component is not allowlisted: {component_id}@{version}"
                )
            if import_path != trusted.import_path or source_commit_sha != trusted.source_commit_sha:
                raise CompatibilityError(
                    f"runtime component provenance mismatch: {component_id}@{version}"
                )
            if component_id in component_ids:
                raise CompatibilityError("runtime blueprint has duplicate component IDs")
            component_ids.add(component_id)
            component_order.append(component_id)
            fixed = _mapping(
                raw.get("fixed_parameters", {}),
                f"runtime_modules[{index}].fixed_parameters",
            )
            schema = _mapping(
                raw.get("parameter_schema", {}),
                f"runtime_modules[{index}].parameter_schema",
            )
            unknown_fixed = sorted(set(fixed) - set(schema))
            if unknown_fixed:
                raise CompatibilityError(
                    f"fixed parameters are absent from schema for {component_id}: "
                    + ", ".join(unknown_fixed)
                )
            schemas[component_id] = schema
            fixed_by_component[component_id] = dict(fixed)
            rows.append(
                {
                    "class": import_path,
                    "enabled": True,
                    "parameters": fixed_by_component[component_id],
                }
            )
        dynamic: dict[str, dict[str, Any]] = {item: {} for item in component_ids}
        for raw_name, value in parameters.items():
            if not isinstance(raw_name, str):
                raise CompatibilityError("job parameter names must be strings")
            if raw_name in _PROVENANCE_FIELDS or raw_name.startswith("factory."):
                continue
            component_id, separator, parameter_name = raw_name.partition(".")
            if not separator or not parameter_name:
                raise CompatibilityError(
                    f"unrecognized unqualified Research Runner parameter: {raw_name}"
                )
            if component_id not in component_ids:
                raise CompatibilityError(
                    f"job parameter references untrusted component: {raw_name}"
                )
            if parameter_name not in schemas[component_id]:
                raise CompatibilityError(
                    f"job parameter is absent from registered schema: {raw_name}"
                )
            if (
                parameter_name in fixed_by_component[component_id]
                and fixed_by_component[component_id][parameter_name] != value
            ):
                raise CompatibilityError(f"job parameter changes fixed value: {raw_name}")
            dynamic[component_id][parameter_name] = _runtime_parameter_value(
                value,
                schemas[component_id][parameter_name],
                raw_name,
            )
        for row, component_id in zip(rows, component_order, strict=True):
            row["parameters"] = {
                **fixed_by_component[component_id],
                **dynamic[component_id],
            }
        return {"modules": tuple(rows)}

    def _write_modules_configuration(
        self, output_directory: Path, payload: Mapping[str, Any]
    ) -> Path:
        boundary = output_directory / ".strategy-factory"
        boundary.mkdir(parents=True, exist_ok=True)
        if boundary.is_symlink():
            raise CompatibilityError("module configuration boundary cannot be a symlink")
        path = boundary / "modules.yaml"
        content = json.dumps(
            _json_primitive(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise CompatibilityError("modules configuration path is unsafe")
            if path.read_bytes() != content:
                raise CompatibilityError("existing modules configuration differs for the same job")
            return path
        descriptor, temporary = tempfile.mkstemp(dir=boundary, prefix=".modules-", suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return path


def _backtest_market_type(request: StrategyFactoryRequest) -> str:
    exchange = request.exchange.lower().replace("-", "_")
    if request.market_type is not MarketType.LINEAR_FUTURES or exchange not in {
        "binance_usdtm",
        "binance_usdt_m",
    }:
        raise CompatibilityError(
            "official Backtest Engine currently accepts Binance USDT-M perpetual only"
        )
    return "binance-usdt-m-perpetual"


def _local_dataset_path(uri: str) -> Path:
    parsed = urlparse(uri)
    if parsed.scheme not in {"", "file"}:
        raise CompatibilityError("Backtest Engine requires a local dataset URI")
    if parsed.scheme == "file" and parsed.netloc not in {"", "localhost"}:
        raise CompatibilityError("remote file URI authority is forbidden")
    value = unquote(parsed.path) if parsed.scheme == "file" else uri
    raw_path = Path(value).expanduser()
    if raw_path.is_symlink():
        raise CompatibilityError("dataset path must not be a symlink")
    path = raw_path.resolve()
    if not path.is_file():
        raise CompatibilityError("dataset path must be a regular file")
    return path


def _verify_regular_file_hash(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise CompatibilityError("dataset hash changed before Backtest Engine execution")


def _reject_symlink_chain(path: Path, *, stop: Path) -> None:
    current = path
    while current != stop:
        if current.exists() and current.is_symlink():
            raise CompatibilityError("job output path contains a symlink")
        if current.parent == current:
            raise CompatibilityError("job output path does not descend from allowed root")
        current = current.parent


def _job_seed(job: Any) -> int:
    value = getattr(job, "seed", None)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CompatibilityError("Research Runner job seed must be an int")
    return value


def _utc_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompatibilityError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CompatibilityError(f"{name} must be a canonical non-blank string")
    return value


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CompatibilityError(f"{name} must be a mapping")
    if any(not isinstance(key, str) for key in value):
        raise CompatibilityError(f"{name} keys must be strings")
    return MappingProxyType(dict(value))


def _runtime_parameter_value(value: Any, schema: Any, name: str) -> Any:
    if not isinstance(value, Decimal):
        return value
    if not isinstance(schema, Mapping) or schema.get("type") != "number":
        return value
    if not value.is_finite():
        raise CompatibilityError(f"{name} must be a finite number")
    numeric = float(value)
    if not math.isfinite(numeric) or Decimal(str(numeric)) != value:
        raise CompatibilityError(
            f"{name} cannot be represented exactly as a numeric JSON primitive"
        )
    return numeric


def _json_primitive(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CompatibilityError("module configuration contains non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CompatibilityError("module configuration contains non-finite Decimal")
        return format(value, "f")
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _json_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): _json_primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_primitive(item) for item in value]
    raise CompatibilityError(
        f"module configuration contains unsupported value: {type(value).__name__}"
    )


def _typed_dataclass_mapping(value: Any, label: str) -> dict[str, Any]:
    if not is_dataclass(value) or isinstance(value, type):
        raise CompatibilityError(f"effective {label} configuration must be a typed dataclass")
    return {item.name: _json_primitive(getattr(value, item.name)) for item in fields(value)}


def _effective_timestamp(value: Any, label: str) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise CompatibilityError(f"effective {label} must be timezone-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _path_identity_sha256(value: Any) -> str | None:
    """Bind a typed path without serializing a potentially sensitive filesystem path."""

    if value is None:
        return None
    if not isinstance(value, str | os.PathLike):
        raise CompatibilityError("effective config path identity is malformed")
    try:
        encoded = os.fsencode(os.fspath(value))
    except (TypeError, UnicodeEncodeError) as exc:
        raise CompatibilityError("effective config path identity is malformed") from exc
    return hashlib.sha256(encoded).hexdigest()


def _job_text(job: Any, name: str) -> str:
    value = getattr(job, name, None)
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CompatibilityError(f"Research Runner job {name} must be canonical")
    return value


def _job_int(job: Any, name: str) -> int:
    value = getattr(job, name, None)
    if not isinstance(value, int) or isinstance(value, bool):
        raise CompatibilityError(f"Research Runner job {name} must be an integer")
    return value
