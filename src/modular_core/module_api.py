from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import TypeVar, cast

from .features import Feature, FeatureDelete, FeatureView
from .manifest import ModuleManifest
from .models import (
    CancelRequest,
    Candle,
    ClosePositionRequest,
    OrderEvent,
    OrderRequest,
    OrderSnapshot,
    PositionSnapshot,
    PositionUpdateRequest,
    SetupEvent,
    Signal,
    Trade,
)
from .readiness import ReadinessView
from .serialization import deep_freeze
from .sessions import SessionView
from .validation import finite_number, require_nonblank

_T = TypeVar("_T")
_EMPTY_TUPLE: tuple[object, ...] = ()
_EMPTY_DIAGNOSTICS: Mapping[str, object] = MappingProxyType({})


@dataclass(frozen=True, slots=True)
class MarketView:
    """Read-only closed-bar view. No future candles or mutable broker objects are exposed."""

    base_bars: Sequence[Candle]
    timeframe_bars: Mapping[str, Sequence[Candle]]
    index: int
    cash: float
    equity: float
    position: PositionSnapshot | None
    pending_orders: tuple[OrderSnapshot, ...]
    exit_pending: bool = False
    data_gap: bool = False
    features: FeatureView | None = None
    readiness: ReadinessView | None = None
    sessions: SessionView | None = None
    order_book: object | None = None
    trade_tape: tuple[object, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise TypeError("index must be int")
        if self.index < 0:
            raise ValueError("index cannot be negative")
        object.__setattr__(self, "cash", finite_number(self.cash, "cash"))
        object.__setattr__(self, "equity", finite_number(self.equity, "equity"))
        _require_bool(self.exit_pending, "exit_pending")
        _require_bool(self.data_gap, "data_gap")
        if self.position is not None and not isinstance(self.position, PositionSnapshot):
            raise TypeError("position must be PositionSnapshot or None")
        if self.features is not None and not callable(getattr(self.features, "get_all", None)):
            raise TypeError("features must provide get_all() or be None")
        if self.readiness is not None and not isinstance(self.readiness, ReadinessView):
            raise TypeError("readiness must be ReadinessView or None")
        if self.sessions is not None and not isinstance(self.sessions, SessionView):
            raise TypeError("sessions must be SessionView or None")
        object.__setattr__(
            self,
            "base_bars",
            _freeze_candle_series(self.base_bars, "base_bars"),
        )
        if not isinstance(self.timeframe_bars, Mapping):
            raise TypeError("timeframe_bars must be a mapping")
        frozen_timeframes: dict[str, tuple[Candle, ...]] = {}
        for timeframe, bars in self.timeframe_bars.items():
            require_nonblank(timeframe, "timeframe_bars key")
            frozen_timeframes[timeframe] = _freeze_candle_series(
                bars,
                f"timeframe_bars[{timeframe!r}]",
            )
        object.__setattr__(
            self,
            "timeframe_bars",
            MappingProxyType(frozen_timeframes),
        )
        object.__setattr__(
            self,
            "pending_orders",
            _typed_tuple(self.pending_orders, OrderSnapshot, "pending_orders"),
        )
        object.__setattr__(self, "trade_tape", _plain_tuple(self.trade_tape, "trade_tape"))

    @classmethod
    def _from_engine_snapshots(
        cls,
        *,
        base_bars: Sequence[Candle],
        timeframe_bars: Mapping[str, Sequence[Candle]],
        index: int,
        cash: float,
        equity: float,
        position: PositionSnapshot | None,
        pending_orders: tuple[OrderSnapshot, ...],
        exit_pending: bool,
        data_gap: bool,
        features: FeatureView | None,
        readiness: ReadinessView | None,
        sessions: SessionView | None,
    ) -> MarketView:
        """Construct a view from immutable snapshots validated by the engine."""
        view = object.__new__(cls)
        object.__setattr__(view, "base_bars", base_bars)
        object.__setattr__(view, "timeframe_bars", timeframe_bars)
        object.__setattr__(view, "index", index)
        object.__setattr__(view, "cash", cash)
        object.__setattr__(view, "equity", equity)
        object.__setattr__(view, "position", position)
        object.__setattr__(view, "pending_orders", pending_orders)
        object.__setattr__(view, "exit_pending", exit_pending)
        object.__setattr__(view, "data_gap", data_gap)
        object.__setattr__(view, "features", features)
        object.__setattr__(view, "readiness", readiness)
        object.__setattr__(view, "sessions", sessions)
        object.__setattr__(view, "order_book", None)
        object.__setattr__(view, "trade_tape", ())
        return view

    @property
    def bars(self) -> Sequence[Candle]:
        return self.base_bars

    @property
    def current(self) -> Candle:
        return self.base_bars[-1]

    @property
    def pending_order_count(self) -> int:
        return len(self.pending_orders)

    def history(
        self,
        length: int,
        *,
        include_current: bool = True,
        timeframe: str | None = None,
    ) -> tuple[Candle, ...]:
        if not isinstance(length, int) or isinstance(length, bool):
            raise TypeError("length must be int")
        _require_bool(include_current, "include_current")
        if timeframe is not None:
            require_nonblank(timeframe, "timeframe")
        if length <= 0:
            return ()
        series = self.base_bars if timeframe is None else self.timeframe_bars.get(timeframe, ())
        end = len(series) if include_current else max(0, len(series) - 1)
        start = max(0, end - length)
        return tuple(series[start:end])

    def current_for(self, timeframe: str) -> Candle | None:
        require_nonblank(timeframe, "timeframe")
        series = self.timeframe_bars.get(timeframe, ())
        return series[-1] if series else None


@dataclass(frozen=True, slots=True)
class ModuleDecision:
    setups: tuple[SetupEvent, ...] = ()
    signals: tuple[Signal, ...] = ()
    features: tuple[Feature, ...] = ()
    feature_deletes: tuple[FeatureDelete, ...] = ()
    orders: tuple[OrderRequest, ...] = ()
    cancellations: tuple[CancelRequest, ...] = ()
    position_updates: tuple[PositionUpdateRequest, ...] = ()
    closes: tuple[ClosePositionRequest, ...] = ()
    skips: tuple[str, ...] = ()
    diagnostics: Mapping[str, object] = field(default_factory=lambda: _EMPTY_DIAGNOSTICS)

    def __post_init__(self) -> None:
        if (
            self.setups is _EMPTY_TUPLE
            and self.signals is _EMPTY_TUPLE
            and self.features is _EMPTY_TUPLE
            and self.feature_deletes is _EMPTY_TUPLE
            and self.orders is _EMPTY_TUPLE
            and self.cancellations is _EMPTY_TUPLE
            and self.position_updates is _EMPTY_TUPLE
            and self.closes is _EMPTY_TUPLE
            and self.skips is _EMPTY_TUPLE
            and self.diagnostics is _EMPTY_DIAGNOSTICS
        ):
            return
        object.__setattr__(self, "setups", _typed_tuple(self.setups, SetupEvent, "setups"))
        object.__setattr__(self, "signals", _typed_tuple(self.signals, Signal, "signals"))
        object.__setattr__(self, "features", _typed_tuple(self.features, Feature, "features"))
        object.__setattr__(
            self,
            "feature_deletes",
            _typed_tuple(self.feature_deletes, FeatureDelete, "feature_deletes"),
        )
        object.__setattr__(self, "orders", _typed_tuple(self.orders, OrderRequest, "orders"))
        object.__setattr__(
            self,
            "cancellations",
            _typed_tuple(self.cancellations, CancelRequest, "cancellations"),
        )
        object.__setattr__(
            self,
            "position_updates",
            _typed_tuple(
                self.position_updates,
                PositionUpdateRequest,
                "position_updates",
            ),
        )
        object.__setattr__(
            self,
            "closes",
            _typed_tuple(self.closes, ClosePositionRequest, "closes"),
        )
        skips = _typed_tuple(self.skips, str, "skips")
        for reason in skips:
            require_nonblank(reason, "skip reason")
        object.__setattr__(self, "skips", skips)
        if not isinstance(self.diagnostics, Mapping):
            raise TypeError("diagnostics must be a mapping")
        if not self.diagnostics:
            object.__setattr__(self, "diagnostics", _EMPTY_DIAGNOSTICS)
            return
        for key in self.diagnostics:
            require_nonblank(key, "diagnostic key")
        frozen_diagnostics = deep_freeze(self.diagnostics)
        object.__setattr__(
            self,
            "diagnostics",
            cast(Mapping[str, object], frozen_diagnostics),
        )

    @property
    def is_empty(self) -> bool:
        return not (
            self.setups
            or self.signals
            or self.features
            or self.feature_deletes
            or self.orders
            or self.cancellations
            or self.position_updates
            or self.closes
            or self.skips
            or self.diagnostics
        )

    @property
    def changes_view_state(self) -> bool:
        return bool(
            self.features
            or self.feature_deletes
            or self.orders
            or self.cancellations
            or self.position_updates
            or self.closes
        )


class StrategyModule(ABC):
    """Module API v1 draft, revision 3.

    Modules communicate only through immutable snapshots, versioned features
    and explicit decisions. New modules do not require kernel changes.
    """

    name: str
    manifest: ModuleManifest | None = None
    parameters: Mapping[str, object] = MappingProxyType({})

    def on_start(self) -> None:
        pass

    @abstractmethod
    def on_bar(self, view: MarketView) -> ModuleDecision:
        raise NotImplementedError

    def on_order_event(self, event: OrderEvent) -> None:
        pass

    def on_trade(self, trade: Trade) -> None:
        pass

    def on_finish(self) -> None:
        pass

    def snapshot_state(self) -> Mapping[str, object]:
        """Return JSON-compatible module state for checkpoints."""
        return {}

    def restore_state(self, state: Mapping[str, object]) -> None:
        """Restore a state produced by ``snapshot_state``."""
        if state:
            raise ValueError(f"module {self.name} does not implement state restoration")


def normalize_modules(
    modules: StrategyModule | Sequence[StrategyModule],
) -> tuple[StrategyModule, ...]:
    result: tuple[StrategyModule, ...]
    if isinstance(modules, StrategyModule):
        result = (modules,)
    else:
        if isinstance(modules, (str, bytes)) or not isinstance(modules, Sequence):
            raise TypeError("modules must be a StrategyModule or sequence")
        result = tuple(modules)
    if not result:
        raise ValueError("at least one strategy module is required")
    for module in result:
        if not isinstance(module, StrategyModule):
            raise TypeError("every module must implement StrategyModule")
        require_nonblank(module.name, "module name")
    names = [module.name for module in result]
    if len(set(names)) != len(names):
        raise ValueError("module names must be unique")
    return result


class _EngineCandleSeries(tuple[Candle, ...]):
    """Validated immutable candle tuple shared by all module views for one bar."""

    __slots__ = ()


def _freeze_candle_series(value: object, name: str) -> tuple[Candle, ...]:
    if isinstance(value, _EngineCandleSeries):
        return value
    return _EngineCandleSeries(_typed_tuple(value, Candle, name))


def _typed_tuple(value: object, expected: type[_T], name: str) -> tuple[_T, ...]:
    if isinstance(value, tuple):
        for item in value:
            if not isinstance(item, expected):
                raise TypeError(f"{name} must contain {expected.__name__}")
        return cast(tuple[_T, ...], value)
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    result: list[_T] = []
    for item in value:
        if not isinstance(item, expected):
            raise TypeError(f"{name} must contain {expected.__name__}")
        result.append(item)
    return tuple(result)


def _plain_tuple(value: object, name: str) -> tuple[object, ...]:
    if isinstance(value, tuple):
        return value
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise TypeError(f"{name} must be a sequence")
    return tuple(value)


def _require_bool(value: object, name: str) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{name} must be bool")
