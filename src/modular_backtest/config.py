from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from modular_core.config import DataConfig, EngineConfig, ExecutionConfig, FeeConfig, RiskConfig
from modular_core.instruments import InstrumentSpec

_ALLOWED_TOP_LEVEL = {
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
_ALLOWED_DATA = {
    "path",
    "format",
    "symbol",
    "market_type",
    "base_timeframe",
    "gap_policy",
    "timestamp_column",
    "columns",
    "start",
    "end",
    "storage_root",
    "dataset_id",
    "dataset_version",
    "registered_mode",
    "stream_batch_size",
}
_REQUIRED_DATA = {"symbol"}
_ALLOWED_EXECUTION = {
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
_ALLOWED_FEES = {"maker_rate", "taker_rate"}
_ALLOWED_RISK = {
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
_ALLOWED_INSTRUMENT = {
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
_ALLOWED_OUTPUT = {"directory"}
_ALLOWED_CHECKPOINT = {"mode", "restart_at_bar"}
_BINANCE_USDT_M_SYMBOL = re.compile(r"^[A-Z0-9]{2,30}USDT$")
_REGISTERED_DATASET_MODES = frozenset({"legacy_materialized", "streaming"})
_DEFAULT_REGISTERED_STREAM_BATCH_SIZE = 8_192


class ConfigurationError(ValueError):
    """Raised when application configuration is unsafe or ambiguous."""


@dataclass(frozen=True, slots=True)
class DataSourceConfig:
    symbol: str
    path: Path | None = None
    format: str | None = None
    market_type: str = "binance-usdt-m-perpetual"
    base_timeframe: str = "5m"
    gap_policy: str = "reject"
    timestamp_column: str = "open_time"
    start: datetime | None = None
    end: datetime | None = None
    columns: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType(
            {
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
            }
        )
    )
    storage_root: Path | None = None
    dataset_id: str | None = None
    dataset_version: int | None = None
    registered_mode: str = "legacy_materialized"
    stream_batch_size: int | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.symbol, str)
            or _BINANCE_USDT_M_SYMBOL.fullmatch(self.symbol) is None
        ):
            raise ConfigurationError(
                "symbol must be an uppercase alphanumeric Binance USDT-M symbol"
            )
        file_values = (self.path is not None, self.format is not None)
        registry_values = (
            self.storage_root is not None,
            self.dataset_id is not None,
            self.dataset_version is not None,
        )
        if any(file_values) and any(registry_values):
            raise ConfigurationError("data source must not mix file and registry fields")
        if any(file_values):
            if not all(file_values):
                raise ConfigurationError("file data source requires both path and format")
            if not isinstance(self.path, Path):
                raise ConfigurationError("data.path must be a filesystem path")
            if self.format not in {"csv", "parquet"}:
                raise ConfigurationError("data.format must be csv or parquet")
            if self.registered_mode != "legacy_materialized":
                raise ConfigurationError(
                    "data.registered_mode requires a registered dataset source"
                )
            if self.stream_batch_size is not None:
                raise ConfigurationError(
                    "data.stream_batch_size requires a registered dataset source"
                )
            object.__setattr__(self, "path", self.path.expanduser())
        elif any(registry_values):
            if not all(registry_values):
                raise ConfigurationError(
                    "registry data source requires storage_root, dataset_id and dataset_version"
                )
            if not isinstance(self.storage_root, Path):
                raise ConfigurationError("data.storage_root must be a filesystem path")
            if (
                not isinstance(self.dataset_id, str)
                or re.fullmatch(r"[a-z0-9][a-z0-9._:-]{2,127}", self.dataset_id) is None
            ):
                raise ConfigurationError("data.dataset_id must be a canonical dataset identifier")
            if (
                not isinstance(self.dataset_version, int)
                or isinstance(self.dataset_version, bool)
                or self.dataset_version <= 0
            ):
                raise ConfigurationError("data.dataset_version must be a positive integer")
            if not isinstance(self.registered_mode, str):
                raise ConfigurationError("data.registered_mode must be a string")
            normalized_mode = self.registered_mode.strip().lower()
            if normalized_mode not in _REGISTERED_DATASET_MODES:
                supported = ", ".join(sorted(_REGISTERED_DATASET_MODES))
                raise ConfigurationError(f"data.registered_mode must be one of: {supported}")
            object.__setattr__(self, "registered_mode", normalized_mode)
            if normalized_mode == "legacy_materialized":
                if self.stream_batch_size is not None:
                    raise ConfigurationError(
                        "data.stream_batch_size requires data.registered_mode = streaming"
                    )
            else:
                batch_size = self.stream_batch_size
                if batch_size is None:
                    batch_size = _DEFAULT_REGISTERED_STREAM_BATCH_SIZE
                if (
                    not isinstance(batch_size, int)
                    or isinstance(batch_size, bool)
                    or batch_size <= 0
                ):
                    raise ConfigurationError("data.stream_batch_size must be a positive integer")
                object.__setattr__(self, "stream_batch_size", batch_size)
            object.__setattr__(self, "storage_root", self.storage_root.expanduser())
        else:
            raise ConfigurationError(
                "data source must define either a file or a registered dataset"
            )
        if not isinstance(self.columns, Mapping):
            raise ConfigurationError("data.columns must be a mapping")
        object.__setattr__(self, "columns", MappingProxyType(dict(self.columns)))
        if self.start is not None:
            object.__setattr__(self, "start", _utc_datetime(self.start, "data.start"))
        if self.end is not None:
            object.__setattr__(self, "end", _utc_datetime(self.end, "data.end"))
        if self.start is not None and self.end is not None and self.end <= self.start:
            raise ConfigurationError("data.end must be later than data.start")
        required_columns = {"open", "high", "low", "close", "volume"}
        if set(self.columns) != required_columns:
            raise ConfigurationError(
                "data.columns must define exactly open, high, low, close and volume"
            )
        physical_columns = [self.timestamp_column, *self.columns.values()]
        if any(not isinstance(name, str) or not name.strip() for name in physical_columns):
            raise ConfigurationError("data column names must be non-empty strings")
        if len(set(physical_columns)) != len(physical_columns):
            raise ConfigurationError(
                "data timestamp and OHLCV columns must map to distinct physical columns"
            )
        if self.market_type != "binance-usdt-m-perpetual":
            raise ConfigurationError(
                "v0.1 accepts only Binance USDT-M perpetual futures; "
                "spot/futures mixing is forbidden"
            )
        if self.base_timeframe != "5m":
            raise ConfigurationError("v0.1 requires 5m as the base timeframe")
        if self.gap_policy not in {"allow", "reject"}:
            raise ConfigurationError("data.gap_policy must be allow or reject")

    @property
    def is_registered(self) -> bool:
        return self.storage_root is not None


@dataclass(frozen=True, slots=True)
class OutputConfig:
    directory: Path = Path("results")

    def __post_init__(self) -> None:
        if not isinstance(self.directory, Path):
            raise ConfigurationError("output.directory must be a filesystem path")
        object.__setattr__(self, "directory", self.directory.expanduser())


@dataclass(frozen=True, slots=True)
class CheckpointConfig:
    mode: str = "disabled"
    restart_at_bar: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in {"disabled", "replay"}:
            raise ConfigurationError("checkpoint.mode must be disabled or replay")
        if self.restart_at_bar is not None:
            if not isinstance(self.restart_at_bar, int) or isinstance(self.restart_at_bar, bool):
                raise ConfigurationError("checkpoint.restart_at_bar must be an integer")
            if self.restart_at_bar < 0:
                raise ConfigurationError("checkpoint.restart_at_bar must be non-negative")
            if self.mode == "disabled":
                raise ConfigurationError(
                    "checkpoint.restart_at_bar requires checkpoint.mode = replay"
                )


@dataclass(frozen=True, slots=True)
class BacktestAppConfig:
    data: DataSourceConfig
    modules: Path
    initial_capital: float = 10_000.0
    requested_timeframes: tuple[str, ...] = ("15m", "1h", "4h", "1d")
    execution: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    fees: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    risk: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    instrument: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    output: OutputConfig = field(default_factory=OutputConfig)
    seed: int = 0
    checkpoint: CheckpointConfig = field(default_factory=CheckpointConfig)
    end_of_test_policy: str = "force_close"

    def __post_init__(self) -> None:
        if not isinstance(self.modules, Path):
            raise ConfigurationError("modules must be a filesystem path")
        object.__setattr__(self, "modules", self.modules.expanduser())
        for name in ("execution", "fees", "risk", "instrument"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise ConfigurationError(f"{name} must be a mapping")
            object.__setattr__(self, name, MappingProxyType(dict(value)))
        valid_capital = (
            isinstance(self.initial_capital, int | float)
            and not isinstance(self.initial_capital, bool)
            and math.isfinite(float(self.initial_capital))
            and self.initial_capital > 0
        )
        if not valid_capital:
            raise ConfigurationError("initial_capital must be a finite positive number")
        object.__setattr__(self, "initial_capital", float(self.initial_capital))
        if not isinstance(self.requested_timeframes, tuple):
            raise ConfigurationError("requested_timeframes must be a sequence of strings")
        if any(
            not isinstance(value, str) or not value.strip() for value in self.requested_timeframes
        ):
            raise ConfigurationError("requested_timeframes must contain non-empty strings")
        if len(set(self.requested_timeframes)) != len(self.requested_timeframes):
            raise ConfigurationError("requested_timeframes must be unique")
        allowed_timeframes = {"15m", "1h", "4h", "1d"}
        unknown = set(self.requested_timeframes) - allowed_timeframes
        if unknown:
            raise ConfigurationError(f"unsupported requested_timeframes: {sorted(unknown)}")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise ConfigurationError("seed must be an integer")
        if self.end_of_test_policy not in {"force_close", "leave_open"}:
            raise ConfigurationError("end_of_test_policy must be force_close or leave_open")

    def core_engine_config(self) -> EngineConfig:
        return EngineConfig(
            initial_cash=self.initial_capital,
            close_open_position_at_end=self.end_of_test_policy == "force_close",
            fees=FeeConfig(**self.fees),
            execution=ExecutionConfig(**self.execution),
            risk=RiskConfig(**self.risk),
            data=DataConfig(
                expected_interval_seconds=300,
                gap_policy=self.data.gap_policy,
                timestamp_semantics="open",
            ),
        )

    def instrument_spec(self) -> InstrumentSpec:
        values = dict(self.instrument)
        market_type = values.setdefault("market_type", "linear_futures")
        if self.data.market_type == "binance-usdt-m-perpetual" and market_type != "linear_futures":
            raise ConfigurationError(
                "Binance USDT-M perpetual data requires instrument.market_type = linear_futures"
            )
        return InstrumentSpec(symbol=self.data.symbol, **values)


def _utc_datetime(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ConfigurationError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _optional_datetime(value: object, name: str) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str | datetime):
        raise ConfigurationError(f"{name} must be an ISO-8601 timestamp")
    if isinstance(value, datetime):
        return _utc_datetime(value, name)
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConfigurationError(f"{name} must be an ISO-8601 timestamp") from exc
    return _utc_datetime(parsed, name)


def _mapping(value: object, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a mapping")
    return dict(value)


def _reject_unknown(mapping: Mapping[str, Any], allowed: set[str], path: str) -> None:
    unknown = set(mapping) - allowed
    if unknown:
        raise ConfigurationError(f"unknown keys in {path}: {sorted(unknown)}")


def _require_keys(mapping: Mapping[str, Any], required: set[str], path: str) -> None:
    missing = sorted(required - set(mapping))
    if missing:
        raise ConfigurationError(f"missing required keys in {path}: {missing}")


def _yaml_path(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-empty path string")
    return value


def _initial_capital(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ConfigurationError("initial_capital must be a finite positive number")
    capital = float(value)
    if not math.isfinite(capital) or capital <= 0:
        raise ConfigurationError("initial_capital must be a finite positive number")
    return capital


def _timeframes(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or isinstance(value, str | bytes):
        raise ConfigurationError("requested_timeframes must be a sequence of strings")
    if any(not isinstance(item, str) or not item.strip() for item in value):
        raise ConfigurationError("requested_timeframes must contain non-empty strings")
    return tuple(value)


def load_config(path: str | Path) -> BacktestAppConfig:
    config_path = Path(path).expanduser().resolve()
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"cannot read valid YAML configuration: {config_path}") from exc
    root = _mapping(payload, "configuration")
    _reject_unknown(root, _ALLOWED_TOP_LEVEL, "configuration")

    data_payload = _mapping(root.get("data"), "data")
    _reject_unknown(data_payload, _ALLOWED_DATA, "data")
    _require_keys(data_payload, _REQUIRED_DATA, "data")
    file_keys = {"path", "format"}
    if file_keys.intersection(data_payload) and not file_keys.issubset(data_payload):
        missing = sorted(file_keys - set(data_payload))
        raise ConfigurationError(f"data missing required keys: {missing}")
    columns = _mapping(data_payload.get("columns", {}), "data.columns")
    data_payload["columns"] = columns or {
        "open": "open",
        "high": "high",
        "low": "low",
        "close": "close",
        "volume": "volume",
    }
    if "path" in data_payload:
        data_path = _yaml_path(data_payload["path"], "data.path")
        data_payload["path"] = (config_path.parent / data_path).resolve()
    else:
        data_payload["path"] = None
    if "storage_root" in data_payload:
        storage_root = _yaml_path(data_payload["storage_root"], "data.storage_root")
        data_payload["storage_root"] = (config_path.parent / storage_root).resolve()
    else:
        data_payload["storage_root"] = None
    data_payload["format"] = data_payload.get("format")
    data_payload["dataset_id"] = data_payload.get("dataset_id")
    data_payload["dataset_version"] = data_payload.get("dataset_version")
    data_payload["registered_mode"] = data_payload.get(
        "registered_mode",
        "legacy_materialized",
    )
    data_payload["stream_batch_size"] = data_payload.get("stream_batch_size")
    data_payload["start"] = _optional_datetime(data_payload.get("start"), "data.start")
    data_payload["end"] = _optional_datetime(data_payload.get("end"), "data.end")
    data = DataSourceConfig(**data_payload)

    modules_value = _yaml_path(root.get("modules"), "modules")
    modules_path = (config_path.parent / modules_value).resolve()

    execution = _mapping(root.get("execution", {}), "execution")
    fees = _mapping(root.get("fees", {}), "fees")
    risk = _mapping(root.get("risk", {}), "risk")
    instrument = _mapping(root.get("instrument", {}), "instrument")
    output_payload = _mapping(root.get("output", {}), "output")
    checkpoint_payload = _mapping(root.get("checkpoint", {}), "checkpoint")
    _reject_unknown(execution, _ALLOWED_EXECUTION, "execution")
    _reject_unknown(fees, _ALLOWED_FEES, "fees")
    _reject_unknown(risk, _ALLOWED_RISK, "risk")
    _reject_unknown(instrument, _ALLOWED_INSTRUMENT, "instrument")
    _reject_unknown(output_payload, _ALLOWED_OUTPUT, "output")
    _reject_unknown(checkpoint_payload, _ALLOWED_CHECKPOINT, "checkpoint")
    output_dir = _yaml_path(output_payload.get("directory", "results"), "output.directory")
    output = OutputConfig((config_path.parent / output_dir).resolve())
    checkpoint = CheckpointConfig(
        mode=str(checkpoint_payload.get("mode", "disabled")),
        restart_at_bar=checkpoint_payload.get("restart_at_bar"),
    )

    return BacktestAppConfig(
        data=data,
        modules=modules_path,
        initial_capital=_initial_capital(root.get("initial_capital", 10_000.0)),
        requested_timeframes=_timeframes(
            root.get("requested_timeframes", ("15m", "1h", "4h", "1d"))
        ),
        execution=execution,
        fees=fees,
        risk=risk,
        instrument=instrument,
        output=output,
        seed=root.get("seed", 0),
        checkpoint=checkpoint,
        end_of_test_policy=str(root.get("end_of_test_policy", "force_close")),
    )
