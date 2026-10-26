from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import datetime
from types import MappingProxyType
from typing import Any

from modular_core.manifest import ModuleManifest
from modular_core.models import Candle
from modular_core.run_manifest import RunManifest, _hash
from modular_core.validation import require_nonblank, utc_datetime


def _canonical_float(value: float) -> float:
    if not math.isfinite(value):
        raise ValueError("run manifest cannot hash non-finite candle values")
    return 0.0 if value == 0.0 else value


class CandleDataHasher:
    """Incrementally reproduce Core's canonical candle-list SHA-256."""

    __slots__ = ("_digest", "_count", "_symbol", "_timeframe", "_finished")

    def __init__(self) -> None:
        self._digest = hashlib.sha256()
        self._digest.update(b"[")
        self._count = 0
        self._symbol: str | None = None
        self._timeframe: str | None = None
        self._finished = False

    @property
    def count(self) -> int:
        return self._count

    @property
    def symbol(self) -> str | None:
        return self._symbol

    @property
    def timeframe(self) -> str | None:
        return self._timeframe

    def update(self, bar: Candle) -> None:
        if self._finished:
            raise RuntimeError("candle hasher is already finalized")
        if not isinstance(bar, Candle):
            raise TypeError("run manifest bars must contain Candle")
        if self._symbol is None:
            self._symbol = bar.symbol
            self._timeframe = bar.timeframe
        elif bar.symbol != self._symbol or bar.timeframe != self._timeframe:
            raise ValueError("run manifest bars must share symbol and timeframe")
        if self._count:
            self._digest.update(b",")
        row = (
            bar.timestamp.isoformat(),
            bar.close_time.isoformat(),
            _canonical_float(bar.open),
            _canonical_float(bar.high),
            _canonical_float(bar.low),
            _canonical_float(bar.close),
            _canonical_float(bar.volume),
            bar.symbol,
            bar.timeframe,
        )
        self._digest.update(
            json.dumps(
                row,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ).encode("utf-8")
        )
        self._count += 1

    def hexdigest(self) -> str:
        if self._count < 1:
            raise ValueError("run manifest requires at least one candle")
        if not self._finished:
            self._digest.update(b"]")
            self._finished = True
        return self._digest.hexdigest()


def incremental_candle_data_hash(bars: Sequence[Candle]) -> str:
    """Reproduce Core's candle data hash without a tuple/list/JSON-size copy."""

    hasher = CandleDataHasher()
    for bar in bars:
        hasher.update(bar)
    return hasher.hexdigest()


def build_scalable_run_manifest_from_hash(
    *,
    kernel_version: str,
    created_at: datetime,
    data_hash: str,
    symbol: str,
    base_timeframe: str,
    bars: int,
    config: Any,
    manifests: tuple[ModuleManifest, ...],
    module_parameters: tuple[Mapping[str, Any], ...] = (),
) -> RunManifest:
    require_nonblank(kernel_version, "kernel_version")
    require_nonblank(data_hash, "data_hash")
    require_nonblank(symbol, "symbol")
    require_nonblank(base_timeframe, "base_timeframe")
    if not isinstance(bars, int) or isinstance(bars, bool):
        raise TypeError("bars must be int")
    if bars < 1:
        raise ValueError("bars must be positive")
    normalized_created_at = utc_datetime(created_at, "run manifest created_at")
    if module_parameters and len(module_parameters) != len(manifests):
        raise ValueError("module_parameters must align with manifests")
    resolved_parameters = module_parameters or tuple(MappingProxyType({}) for _ in manifests)
    config_hash = _hash(config)
    module_hash = _hash(tuple(zip(manifests, resolved_parameters)))
    identity = _hash((kernel_version, data_hash, config_hash, module_hash))[:16]
    return RunManifest(
        run_id=f"run-{identity}",
        kernel_version=kernel_version,
        created_at=normalized_created_at,
        data_hash=data_hash,
        config_hash=config_hash,
        module_hash=module_hash,
        symbol=symbol,
        base_timeframe=base_timeframe,
        bars=bars,
        module_manifests=manifests,
        module_parameters=resolved_parameters,
    )


def build_scalable_run_manifest(
    *,
    kernel_version: str,
    created_at: datetime,
    bars: Sequence[Candle],
    config: Any,
    manifests: tuple[ModuleManifest, ...],
    module_parameters: tuple[Mapping[str, Any], ...] = (),
) -> RunManifest:
    """Build the exact Core manifest while hashing candle data incrementally."""

    if not bars:
        raise ValueError("run manifest requires at least one candle")
    return build_scalable_run_manifest_from_hash(
        kernel_version=kernel_version,
        created_at=created_at,
        data_hash=incremental_candle_data_hash(bars),
        symbol=bars[0].symbol,
        base_timeframe=bars[0].timeframe,
        bars=len(bars),
        config=config,
        manifests=manifests,
        module_parameters=module_parameters,
    )
