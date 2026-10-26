from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import MISSING, dataclass, fields, is_dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import math
from types import MappingProxyType
from typing import Any, cast

from .data import timeframe_to_seconds
from .manifest import ModuleManifest
from .models import Candle
from .serialization import deep_freeze
from .validation import require_nonblank, utc_datetime

_RUN_IDENTITY_OMIT_DEFAULT = "run_identity_omit_default"


@dataclass(frozen=True, slots=True)
class RunManifest:
    run_id: str
    kernel_version: str
    created_at: datetime
    data_hash: str
    config_hash: str
    module_hash: str
    symbol: str
    base_timeframe: str
    bars: int
    module_manifests: tuple[ModuleManifest, ...]
    module_parameters: tuple[Mapping[str, Any], ...]

    def __post_init__(self) -> None:
        require_nonblank(self.run_id, "run_id")
        kernel_version = require_nonblank(self.kernel_version, "kernel_version")
        data_hash = require_nonblank(self.data_hash, "data_hash")
        config_hash = require_nonblank(self.config_hash, "config_hash")
        module_hash = require_nonblank(self.module_hash, "module_hash")
        symbol = require_nonblank(self.symbol, "symbol")
        base_timeframe = require_nonblank(self.base_timeframe, "base_timeframe")
        if timeframe_to_seconds(base_timeframe) is None:
            raise ValueError(f"invalid base timeframe: {base_timeframe!r}")
        if not isinstance(self.bars, int) or isinstance(self.bars, bool):
            raise TypeError("bars must be int")
        if self.bars < 1:
            raise ValueError("bars must be positive")

        manifests = _manifest_tuple(self.module_manifests)
        names = [manifest.name for manifest in manifests]
        if len(names) != len(set(names)):
            raise ValueError("module manifests must have unique names")
        parameters = _parameter_tuple(self.module_parameters)
        if not parameters and manifests:
            parameters = tuple(MappingProxyType({}) for _ in manifests)
        if len(parameters) != len(manifests):
            raise ValueError("module_parameters must align with manifests")

        expected_module_hash = _hash(tuple(zip(manifests, parameters)))
        if module_hash != expected_module_hash:
            raise ValueError(
                "module_hash is inconsistent with manifests and parameters: "
                f"expected {expected_module_hash}"
            )
        expected_run_id = (
            "run-"
            + _hash(
                (
                    kernel_version,
                    data_hash,
                    config_hash,
                    expected_module_hash,
                )
            )[:16]
        )
        if self.run_id != expected_run_id:
            raise ValueError(
                f"run_id is inconsistent with manifest hashes: expected {expected_run_id}"
            )

        object.__setattr__(
            self,
            "created_at",
            utc_datetime(self.created_at, "run manifest created_at"),
        )
        object.__setattr__(self, "kernel_version", kernel_version)
        object.__setattr__(self, "data_hash", data_hash)
        object.__setattr__(self, "config_hash", config_hash)
        object.__setattr__(self, "module_hash", expected_module_hash)
        object.__setattr__(self, "symbol", symbol)
        object.__setattr__(self, "base_timeframe", base_timeframe)
        object.__setattr__(self, "module_manifests", manifests)
        object.__setattr__(self, "module_parameters", parameters)


def build_run_manifest(
    *,
    kernel_version: str,
    created_at: datetime,
    bars: Iterable[Candle],
    config: Any,
    manifests: tuple[ModuleManifest, ...],
    module_parameters: tuple[Mapping[str, Any], ...] = (),
) -> RunManifest:
    require_nonblank(kernel_version, "kernel_version")
    created_at = utc_datetime(created_at, "run manifest created_at")
    bar_list = tuple(bars)
    if not bar_list:
        raise ValueError("run manifest requires at least one candle")
    if any(not isinstance(bar, Candle) for bar in bar_list):
        raise TypeError("run manifest bars must contain Candle")
    if len({bar.symbol for bar in bar_list}) != 1 or len({bar.timeframe for bar in bar_list}) != 1:
        raise ValueError("run manifest bars must share symbol and timeframe")
    manifest_tuple = _manifest_tuple(manifests)
    parameter_tuple = _parameter_tuple(module_parameters)
    if parameter_tuple and len(parameter_tuple) != len(manifest_tuple):
        raise ValueError("module_parameters must align with manifests")
    resolved_parameters = parameter_tuple or tuple(MappingProxyType({}) for _ in manifest_tuple)
    data_hash = _hash(
        [
            (
                bar.timestamp.isoformat(),
                bar.close_time.isoformat(),
                bar.open,
                bar.high,
                bar.low,
                bar.close,
                bar.volume,
                bar.symbol,
                bar.timeframe,
            )
            for bar in bar_list
        ]
    )
    config_hash = _hash(config)
    module_hash = _hash(tuple(zip(manifest_tuple, resolved_parameters)))
    identity = _hash((kernel_version, data_hash, config_hash, module_hash))[:16]
    return RunManifest(
        run_id=f"run-{identity}",
        kernel_version=kernel_version,
        created_at=created_at,
        data_hash=data_hash,
        config_hash=config_hash,
        module_hash=module_hash,
        symbol=bar_list[0].symbol,
        base_timeframe=bar_list[0].timeframe,
        bars=len(bar_list),
        module_manifests=manifest_tuple,
        module_parameters=resolved_parameters,
    )


def _manifest_tuple(value: Sequence[ModuleManifest]) -> tuple[ModuleManifest, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("module_manifests must be a sequence")
    manifests = tuple(value)
    if any(not isinstance(manifest, ModuleManifest) for manifest in manifests):
        raise TypeError("module_manifests must contain ModuleManifest")
    return manifests


def _parameter_tuple(
    value: Sequence[Mapping[str, Any]],
) -> tuple[Mapping[str, Any], ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError("module_parameters must be a sequence")
    result: list[Mapping[str, Any]] = []
    for parameters in value:
        if not isinstance(parameters, Mapping):
            raise TypeError("module_parameters must contain mappings")
        primitive = _primitive(parameters)
        if not isinstance(primitive, dict):
            raise RuntimeError("module parameter evidence must remain a mapping")
        frozen = deep_freeze(primitive)
        if not isinstance(frozen, Mapping):
            raise RuntimeError("frozen module parameter evidence must remain a mapping")
        result.append(cast(Mapping[str, Any], frozen))
    return tuple(result)


def _hash(value: Any) -> str:
    payload = json.dumps(
        _primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _primitive(value: Any) -> Any:
    if is_dataclass(value):
        result: dict[str, Any] = {}
        for item in fields(value):
            item_value = getattr(value, item.name)
            if item.metadata.get(_RUN_IDENTITY_OMIT_DEFAULT) is True:
                if item.default is MISSING:
                    raise RuntimeError(
                        f"run identity default metadata requires a concrete default: {item.name}"
                    )
                if item_value == item.default:
                    continue
            result[item.name] = _primitive(item_value)
        return result
    if isinstance(value, Mapping):
        return {_require_string_key(key): _primitive(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_primitive(item) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("run manifest cannot hash non-finite numbers")
        return 0.0 if value == 0.0 else value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return _primitive(value.value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise TypeError(f"unsupported run manifest value type: {type(value).__name__}")


def _require_string_key(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("run manifest mapping keys must be str")
    return value
