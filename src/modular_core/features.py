from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from types import MappingProxyType
from typing import Any

from .serialization import deep_freeze
from .validation import require_nonblank, utc_datetime


@dataclass(frozen=True, slots=True)
class Feature:
    """Immutable, versioned fact published by a strategy module.

    Features are the only supported way for strategy modules to exchange
    derived state. A feature becomes visible only after it has been published
    by an earlier module in the deterministic module pipeline for the current
    closed bar, or on a previous bar.
    """

    key: str
    source_module: str
    created_at: datetime
    value: Any
    symbol: str = "*"
    timeframe: str | None = None
    valid_until: datetime | None = None
    schema_version: str = "1"
    feature_id: str | None = None
    invalidate_on_gap: bool = True
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_feature_key(self.key)
        require_nonblank(self.source_module, "feature source_module")
        require_nonblank(self.symbol, "feature symbol")
        if self.timeframe is not None:
            require_nonblank(self.timeframe, "feature timeframe")
        if self.feature_id is not None:
            require_nonblank(self.feature_id, "feature_id")
        _require_bool(self.invalidate_on_gap, "feature invalidate_on_gap")
        object.__setattr__(self, "created_at", utc_datetime(self.created_at, "feature created_at"))
        if self.valid_until is not None:
            object.__setattr__(
                self,
                "valid_until",
                utc_datetime(self.valid_until, "feature valid_until"),
            )
            if self.valid_until < self.created_at:
                raise ValueError("feature valid_until cannot be before created_at")
        require_nonblank(self.schema_version, "feature schema_version")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("feature metadata must be a mapping")
        object.__setattr__(self, "value", deep_freeze(self.value))
        object.__setattr__(self, "metadata", deep_freeze(self.metadata))

    def is_active(self, timestamp: datetime) -> bool:
        timestamp = utc_datetime(timestamp, "feature activity timestamp")
        return self.valid_until is None or timestamp <= self.valid_until


@dataclass(frozen=True, slots=True)
class FeatureDelete:
    key: str
    source_module: str
    feature_id: str | None = None
    symbol: str | None = None
    timeframe: str | None = None
    reason: str = "module_invalidation"

    def __post_init__(self) -> None:
        _validate_feature_key(self.key)
        require_nonblank(self.source_module, "feature delete source_module")
        require_nonblank(self.reason, "feature delete reason")
        for name in ("feature_id", "symbol", "timeframe"):
            value = getattr(self, name)
            if value is not None:
                require_nonblank(value, f"feature delete {name}")


class FeatureView:
    """Read-only point-in-time feature snapshot exposed to modules."""

    __slots__ = ("_by_key", "timestamp")

    def __init__(self, features: Iterable[Feature], timestamp: datetime) -> None:
        if isinstance(features, (str, bytes)) or not isinstance(features, Iterable):
            raise TypeError("features must be an iterable of Feature")
        feature_list = tuple(features)
        if any(not isinstance(feature, Feature) for feature in feature_list):
            raise TypeError("features must contain Feature")
        timestamp = utc_datetime(timestamp, "feature snapshot timestamp")
        by_key: dict[str, tuple[Feature, ...]] = {}
        grouped: dict[str, list[Feature]] = {}
        for feature in feature_list:
            if feature.created_at > timestamp or not feature.is_active(timestamp):
                continue
            grouped.setdefault(feature.key, []).append(feature)
        for key, values in grouped.items():
            values.sort(key=lambda x: (x.created_at, x.feature_id or ""))
            by_key[key] = tuple(values)
        self._by_key = MappingProxyType(by_key)
        self.timestamp = timestamp

    def get(
        self,
        key: str,
        default: Any = None,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        source_module: str | None = None,
    ) -> Any:
        feature = self.latest(
            key,
            symbol=symbol,
            timeframe=timeframe,
            source_module=source_module,
        )
        return feature.value if feature is not None else default

    def latest(
        self,
        key: str,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        source_module: str | None = None,
    ) -> Feature | None:
        values = self._filtered(key, symbol, timeframe, source_module)
        return values[-1] if values else None

    def get_all(
        self,
        key: str,
        *,
        symbol: str | None = None,
        timeframe: str | None = None,
        source_module: str | None = None,
    ) -> tuple[Feature, ...]:
        return self._filtered(key, symbol, timeframe, source_module)

    def exists(self, key: str, **filters: Any) -> bool:
        return self.latest(key, **filters) is not None

    def keys(self) -> tuple[str, ...]:
        return tuple(self._by_key)

    def _filtered(
        self,
        key: str,
        symbol: str | None,
        timeframe: str | None,
        source_module: str | None,
    ) -> tuple[Feature, ...]:
        values = self._by_key.get(key, ())
        if symbol is None and timeframe is None and source_module is None:
            return values
        return tuple(
            feature
            for feature in values
            if (symbol is None or feature.symbol in {symbol, "*"})
            and (timeframe is None or feature.timeframe == timeframe)
            and (source_module is None or feature.source_module == source_module)
        )


class FeatureStore:
    """Mutable kernel-owned store; modules receive only FeatureView snapshots."""

    def __init__(self, max_features: int | None = None) -> None:
        if max_features is not None:
            if not isinstance(max_features, int) or isinstance(max_features, bool):
                raise TypeError("max_features must be int or None")
            if max_features < 1:
                raise ValueError("max_features must be >= 1")
        self.max_features = max_features
        self._features: dict[str, Feature] = {}
        self._sequence = 0
        self._revision = 0
        self._cached_snapshot: tuple[datetime, int, FeatureView] | None = None
        self._snapshot_builds = 0

    def publish(self, feature: Feature) -> Feature:
        if not isinstance(feature, Feature):
            raise TypeError("feature must be Feature")
        next_sequence = self._sequence + 1
        feature_id = feature.feature_id or f"feat-{next_sequence:08d}"
        if feature_id in self._features:
            raise ValueError(f"duplicate feature_id: {feature_id}")
        if self.max_features is not None and len(self._features) >= self.max_features:
            raise ValueError("feature store capacity exceeded")
        stored = Feature(
            key=feature.key,
            source_module=feature.source_module,
            created_at=feature.created_at,
            value=feature.value,
            symbol=feature.symbol,
            timeframe=feature.timeframe,
            valid_until=feature.valid_until,
            schema_version=feature.schema_version,
            feature_id=feature_id,
            invalidate_on_gap=feature.invalidate_on_gap,
            metadata=feature.metadata,
        )
        self._features[feature_id] = stored
        self._sequence = next_sequence
        self._touch()
        return stored

    def delete(self, request: FeatureDelete) -> tuple[Feature, ...]:
        if not isinstance(request, FeatureDelete):
            raise TypeError("request must be FeatureDelete")
        removed: list[Feature] = []
        for feature_id, feature in tuple(self._features.items()):
            if feature.key != request.key:
                continue
            if feature.source_module != request.source_module:
                continue
            if request.feature_id is not None and feature.feature_id != request.feature_id:
                continue
            if request.symbol is not None and feature.symbol != request.symbol:
                continue
            if request.timeframe is not None and feature.timeframe != request.timeframe:
                continue
            removed.append(self._features.pop(feature_id))
        if removed:
            self._touch()
        return tuple(removed)

    def expire(self, timestamp: datetime) -> tuple[Feature, ...]:
        timestamp = utc_datetime(timestamp, "feature expiry timestamp")
        removed: list[Feature] = []
        for feature_id, feature in tuple(self._features.items()):
            if feature.valid_until is not None and timestamp > feature.valid_until:
                removed.append(self._features.pop(feature_id))
        if removed:
            self._touch()
        return tuple(removed)

    def delete_source(self, source_module: str) -> tuple[Feature, ...]:
        require_nonblank(source_module, "feature source_module")
        removed = tuple(
            feature for feature in self._features.values() if feature.source_module == source_module
        )
        for feature in removed:
            if feature.feature_id is not None:
                self._features.pop(feature.feature_id, None)
        if removed:
            self._touch()
        return removed

    def invalidate_on_gap(self, *, symbol: str | None = None) -> tuple[Feature, ...]:
        if symbol is not None:
            require_nonblank(symbol, "feature gap symbol")
        removed: list[Feature] = []
        for feature_id, feature in tuple(self._features.items()):
            if not feature.invalidate_on_gap:
                continue
            if symbol is not None and feature.symbol not in {symbol, "*"}:
                continue
            removed.append(self._features.pop(feature_id))
        if removed:
            self._touch()
        return tuple(removed)

    def snapshot(self, timestamp: datetime) -> FeatureView:
        timestamp = utc_datetime(timestamp, "feature snapshot timestamp")
        cached = self._cached_snapshot
        if cached is not None and cached[0] == timestamp and cached[1] == self._revision:
            return cached[2]
        view = FeatureView(tuple(self._features.values()), timestamp)
        self._snapshot_builds += 1
        self._cached_snapshot = (timestamp, self._revision, view)
        return view

    @property
    def count(self) -> int:
        return len(self._features)

    def copy(self) -> FeatureStore:
        clone = FeatureStore(self.max_features)
        clone._features = dict(self._features)
        clone._sequence = self._sequence
        clone._revision = self._revision
        clone._snapshot_builds = self._snapshot_builds
        return clone

    @property
    def snapshot_builds(self) -> int:
        """Number of materialized snapshots, exposed for profiling and tests."""
        return self._snapshot_builds

    def _touch(self) -> None:
        self._revision += 1
        self._cached_snapshot = None

    def all(self) -> tuple[Feature, ...]:
        return tuple(self._features.values())


def _validate_feature_key(key: str) -> None:
    require_nonblank(key, "feature key")
    parts = key.split(".")
    if len(parts) < 2 or any(not part.strip() for part in parts):
        raise ValueError("feature key must contain nonblank namespace segments")


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
