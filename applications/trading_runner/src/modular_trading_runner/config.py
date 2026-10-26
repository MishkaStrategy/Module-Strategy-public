from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import Any

import yaml

from .enums import ExecutionMode
from .errors import ConfigurationError

_ALLOWED_ROOT = frozenset(
    {
        "schema_version",
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "execution_mode",
        "base_timeframe",
        "market",
        "instruments",
        "market_data_sources",
        "modules",
        "shutdown_timeout_seconds",
        "stale_after_seconds",
        "max_future_skew_seconds",
        "max_queue_size",
        "max_history_bars",
        "max_tracked_market_events",
    }
)
_REQUIRED_ROOT = frozenset(
    {
        "schema_version",
        "runtime_id",
        "portfolio_id",
        "strategy_id",
        "execution_mode",
        "base_timeframe",
        "market",
        "instruments",
        "market_data_sources",
        "modules",
    }
)


def _nonblank(value: object, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ConfigurationError(f"{name} cannot contain surrounding whitespace")
    return value


def _nonblank_tuple(value: object, name: str) -> tuple[str, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigurationError(f"{name} must be a sequence")
    result = tuple(_nonblank(item, f"{name} item") for item in value)
    if not result:
        raise ConfigurationError(f"{name} cannot be empty")
    if len(set(result)) != len(result):
        raise ConfigurationError(f"{name} cannot contain duplicates")
    return result


def _positive_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    result = float(value)
    if result <= 0 or not result < float("inf"):
        raise ConfigurationError(f"{name} must be positive and finite")
    return result


def _nonnegative_number(value: object, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigurationError(f"{name} must be a number")
    result = float(value)
    if result < 0 or not result < float("inf"):
        raise ConfigurationError(f"{name} must be non-negative and finite")
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigurationError(f"{name} must be an integer")
    if value < 1:
        raise ConfigurationError(f"{name} must be positive")
    return value


def _module_tuple(value: object) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ConfigurationError("modules must be a sequence")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ConfigurationError(f"modules[{index}] must be a mapping")
        row = dict(item)
        if any(not isinstance(key, str) for key in row):
            raise ConfigurationError(f"modules[{index}] keys must be strings")
        result.append(MappingProxyType(row))
    return tuple(result)


@dataclass(frozen=True, slots=True)
class TradingRunnerConfig:
    schema_version: str
    runtime_id: str
    portfolio_id: str
    strategy_id: str
    execution_mode: ExecutionMode
    base_timeframe: str
    market: str
    instruments: tuple[str, ...]
    market_data_sources: tuple[str, ...]
    modules: tuple[Mapping[str, Any], ...]
    shutdown_timeout_seconds: float
    stale_after_seconds: float
    max_future_skew_seconds: float
    max_queue_size: int
    max_history_bars: int
    max_tracked_market_events: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> TradingRunnerConfig:
        if not isinstance(raw, Mapping):
            raise ConfigurationError("configuration root must be a mapping")
        payload = dict(raw)
        if any(not isinstance(key, str) for key in payload):
            raise ConfigurationError("configuration keys must be strings")
        unknown = sorted(set(payload) - _ALLOWED_ROOT)
        if unknown:
            raise ConfigurationError(f"unknown configuration keys: {', '.join(unknown)}")
        missing = sorted(_REQUIRED_ROOT - set(payload))
        if missing:
            raise ConfigurationError(f"missing configuration keys: {', '.join(missing)}")
        schema_version = _nonblank(payload["schema_version"], "schema_version")
        if schema_version != "1":
            raise ConfigurationError("unsupported configuration schema_version")
        try:
            execution_mode = ExecutionMode(payload["execution_mode"])
        except (TypeError, ValueError) as exc:
            raise ConfigurationError("execution_mode must be exactly 'paper'") from exc
        return cls(
            schema_version=schema_version,
            runtime_id=_nonblank(payload["runtime_id"], "runtime_id"),
            portfolio_id=_nonblank(payload["portfolio_id"], "portfolio_id"),
            strategy_id=_nonblank(payload["strategy_id"], "strategy_id"),
            execution_mode=execution_mode,
            base_timeframe=_nonblank(payload["base_timeframe"], "base_timeframe"),
            market=_nonblank(payload["market"], "market"),
            instruments=_nonblank_tuple(payload["instruments"], "instruments"),
            market_data_sources=_nonblank_tuple(
                payload["market_data_sources"], "market_data_sources"
            ),
            modules=_module_tuple(payload["modules"]),
            shutdown_timeout_seconds=_positive_number(
                payload.get("shutdown_timeout_seconds", 30.0),
                "shutdown_timeout_seconds",
            ),
            stale_after_seconds=_positive_number(
                payload.get("stale_after_seconds", 30.0),
                "stale_after_seconds",
            ),
            max_future_skew_seconds=_nonnegative_number(
                payload.get("max_future_skew_seconds", 2.0),
                "max_future_skew_seconds",
            ),
            max_queue_size=_positive_int(
                payload.get("max_queue_size", 4096),
                "max_queue_size",
            ),
            max_history_bars=_positive_int(
                payload.get("max_history_bars", 4096),
                "max_history_bars",
            ),
            max_tracked_market_events=_positive_int(
                payload.get("max_tracked_market_events", 10000),
                "max_tracked_market_events",
            ),
        )

    @classmethod
    def load_yaml(cls, path: str | Path, *, max_bytes: int = 1024 * 1024) -> TradingRunnerConfig:
        source = Path(path)
        if source.is_symlink():
            raise ConfigurationError("configuration file cannot be a symlink")
        data = source.read_bytes()
        if len(data) > max_bytes:
            raise ConfigurationError("configuration file exceeds size limit")
        try:
            payload = yaml.safe_load(data.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise ConfigurationError(f"invalid YAML configuration: {exc}") from exc
        return cls.from_mapping(payload)

    def canonical_mapping(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "runtime_id": self.runtime_id,
                "portfolio_id": self.portfolio_id,
                "strategy_id": self.strategy_id,
                "execution_mode": self.execution_mode.value,
                "base_timeframe": self.base_timeframe,
                "market": self.market,
                "instruments": self.instruments,
                "market_data_sources": self.market_data_sources,
                "modules": tuple(dict(item) for item in self.modules),
                "shutdown_timeout_seconds": self.shutdown_timeout_seconds,
                "stale_after_seconds": self.stale_after_seconds,
                "max_future_skew_seconds": self.max_future_skew_seconds,
                "max_queue_size": self.max_queue_size,
                "max_history_bars": self.max_history_bars,
                "max_tracked_market_events": self.max_tracked_market_events,
            }
        )

    @property
    def canonical_hash(self) -> str:
        encoded = json.dumps(
            dict(self.canonical_mapping()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        return sha256(encoded).hexdigest()
