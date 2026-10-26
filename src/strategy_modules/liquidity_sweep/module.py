from __future__ import annotations

from collections import deque
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from threading import RLock
from types import MappingProxyType
from typing import Mapping, Self

from modular_core import (
    Candle,
    Feature,
    FeatureDelete,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    SetupEvent,
    Signal,
    StrategyModule,
)

from .config import LiquiditySweepConfig
from .constants import (
    LEVEL_FEATURE_KEY,
    MODULE_API_VERSION,
    MODULE_NAME,
    MODULE_VERSION,
    STATE_SCHEMA_VERSION,
    SWEEP_FEATURE_KEY,
)
from .detector import SweepEvaluator
from .models import BarSnapshot, LiquidityLevel
from .outputs import build_sweep_outputs, level_feature
from .pivots import confirm_levels
from .reasons import LiquiditySweepReason
from .state import empty_stats, ordered_levels, restore_state, snapshot_state


def _decimal(value: float) -> Decimal:
    return Decimal(str(value))


def _utc(value: datetime, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class LiquiditySweepModule(StrategyModule):
    """Detect closed-bar liquidity sweeps of confirmed swing levels.

    A swing level is confirmed only after ``pivot_right_bars`` later candles
    have closed. The module publishes analytical facts and never owns orders,
    risk sizing or position management.
    """

    name = MODULE_NAME

    def __init__(
        self,
        *,
        timeframe: str = "base",
        pivot_left_bars: int = 2,
        pivot_right_bars: int = 2,
        min_level_age_bars: int = 1,
        max_level_age_bars: int = 500,
        max_active_levels: int = 128,
        min_penetration_bps: float = 0.0,
        max_penetration_bps: float = 100.0,
        close_reclaim_bps: float = 0.0,
        invalidation_close_bps: float = 0.0,
        min_wick_to_body_ratio: float = 1.0,
        min_wick_fraction: float = 0.35,
        require_directional_close: bool = False,
        emit_signals: bool = False,
    ) -> None:
        self.config = LiquiditySweepConfig(
            timeframe=timeframe,
            pivot_left_bars=pivot_left_bars,
            pivot_right_bars=pivot_right_bars,
            min_level_age_bars=min_level_age_bars,
            max_level_age_bars=max_level_age_bars,
            max_active_levels=max_active_levels,
            min_penetration_bps=min_penetration_bps,
            max_penetration_bps=max_penetration_bps,
            close_reclaim_bps=close_reclaim_bps,
            invalidation_close_bps=invalidation_close_bps,
            min_wick_to_body_ratio=min_wick_to_body_ratio,
            min_wick_fraction=min_wick_fraction,
            require_directional_close=require_directional_close,
            emit_signals=emit_signals,
        )
        self.parameters = self.config.as_parameters()
        self._config_fingerprint = hashlib.sha256(
            json.dumps(dict(self.parameters), sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        self._evaluator = SweepEvaluator(self.config)
        explicit_timeframe = self.config.timeframe != "base"
        self.manifest = ModuleManifest(
            name=self.name,
            version=MODULE_VERSION,
            api_version=MODULE_API_VERSION,
            required_timeframes=(self.config.timeframe,) if explicit_timeframe else (),
            produced_features=(LEVEL_FEATURE_KEY, SWEEP_FEATURE_KEY),
            warmup_bars=(
                {self.config.timeframe: self.config.pivot_window_bars} if explicit_timeframe else {}
            ),
            parameters_schema=self._parameters_schema(),
            skip_until_ready=False,
            description="Confirmed swing-liquidity level and closed-bar sweep detector.",
            state_schema_version=STATE_SCHEMA_VERSION,
        )
        self._bars: deque[BarSnapshot] = deque(maxlen=self.config.pivot_window_bars)
        self._levels: dict[str, LiquidityLevel] = {}
        self._sequence = 0
        self._last_bar: BarSnapshot | None = None
        self._stats = empty_stats()
        self._needs_feature_republish: set[str] = set()
        self._runtime_restore_count = 0
        self._runtime_republished_levels = 0
        self._lock = RLock()

    @classmethod
    def from_config(cls, config: LiquiditySweepConfig) -> Self:
        if not isinstance(config, LiquiditySweepConfig):
            raise TypeError("config must be LiquiditySweepConfig")
        return cls(
            timeframe=config.timeframe,
            pivot_left_bars=config.pivot_left_bars,
            pivot_right_bars=config.pivot_right_bars,
            min_level_age_bars=config.min_level_age_bars,
            max_level_age_bars=config.max_level_age_bars,
            max_active_levels=config.max_active_levels,
            min_penetration_bps=config.min_penetration_bps,
            max_penetration_bps=config.max_penetration_bps,
            close_reclaim_bps=config.close_reclaim_bps,
            invalidation_close_bps=config.invalidation_close_bps,
            min_wick_to_body_ratio=config.min_wick_to_body_ratio,
            min_wick_fraction=config.min_wick_fraction,
            require_directional_close=config.require_directional_close,
            emit_signals=config.emit_signals,
        )

    @staticmethod
    def _parameters_schema() -> Mapping[str, Mapping[str, object]]:
        return MappingProxyType(
            {
                "timeframe": {"type": "string", "default": "base"},
                "pivot_left_bars": {"type": "integer", "minimum": 1, "default": 2},
                "pivot_right_bars": {"type": "integer", "minimum": 1, "default": 2},
                "min_level_age_bars": {"type": "integer", "minimum": 0, "default": 1},
                "max_level_age_bars": {"type": "integer", "minimum": 0, "default": 500},
                "max_active_levels": {"type": "integer", "minimum": 1, "default": 128},
                "min_penetration_bps": {"type": "number", "minimum": 0, "default": 0.0},
                "max_penetration_bps": {"type": "number", "minimum": 0, "default": 100.0},
                "close_reclaim_bps": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 10000,
                    "default": 0.0,
                },
                "invalidation_close_bps": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 10000,
                    "default": 0.0,
                },
                "min_wick_to_body_ratio": {"type": "number", "minimum": 0, "default": 1.0},
                "min_wick_fraction": {
                    "type": "number",
                    "minimum": 0,
                    "maximum": 1,
                    "default": 0.35,
                },
                "require_directional_close": {"type": "boolean", "default": False},
                "emit_signals": {"type": "boolean", "default": False},
            }
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        with self._lock:
            gap_deletes: list[FeatureDelete] = []
            if view.data_gap:
                gap_deletes.extend(self._delete_all_levels(LiquiditySweepReason.DATA_GAP.value))
                self._bars.clear()
                self._stats["gap_resets"] += 1

            candle = self._select_candle(view)
            if candle is None:
                reason = (
                    LiquiditySweepReason.EMPTY_MARKET_VIEW.value
                    if self.config.timeframe == "base"
                    else LiquiditySweepReason.TIMEFRAME_UNAVAILABLE.value
                )
                skips = [reason]
                if view.data_gap:
                    skips.insert(0, LiquiditySweepReason.DATA_GAP_RESET.value)
                return ModuleDecision(
                    feature_deletes=tuple(gap_deletes),
                    skips=tuple(skips),
                    diagnostics={
                        "state": "waiting_for_timeframe",
                        "timeframe": self.config.timeframe,
                        "gap_reset": view.data_gap,
                        "active_levels": len(self._levels),
                        "totals": dict(self._stats),
                        "runtime_restore_count": self._runtime_restore_count,
                        "runtime_republished_levels": self._runtime_republished_levels,
                    },
                )
            if (
                self.config.timeframe != "base"
                and candle.timeframe.lower() != self.config.timeframe
            ):
                return ModuleDecision(
                    feature_deletes=tuple(gap_deletes),
                    skips=(LiquiditySweepReason.TIMEFRAME_MISMATCH.value,),
                    diagnostics={
                        "state": "rejected_bar",
                        "expected_timeframe": self.config.timeframe,
                        "received_timeframe": candle.timeframe,
                        "gap_reset": view.data_gap,
                        "totals": dict(self._stats),
                    },
                )
            bar = self._snapshot(candle)

            if (
                view.data_gap
                and self._last_bar is not None
                and bar.timestamp <= self._last_bar.timestamp
            ):
                self._last_bar = bar if bar.timestamp == self._last_bar.timestamp else None
                return ModuleDecision(
                    feature_deletes=tuple(gap_deletes),
                    skips=(LiquiditySweepReason.DATA_GAP_RESET.value,),
                    diagnostics={
                        "state": "gap_reset_waiting_for_new_bar",
                        "processed_sequence": self._sequence,
                        "buffered_bars": 0,
                        "active_levels": 0,
                        "totals": dict(self._stats),
                    },
                )

            stream_deletes: list[FeatureDelete] = []
            stream_changed = (
                not view.data_gap
                and self._last_bar is not None
                and bar.timestamp > self._last_bar.timestamp
                and (bar.symbol, bar.timeframe) != (self._last_bar.symbol, self._last_bar.timeframe)
            )
            if stream_changed:
                stream_deletes.extend(
                    self._delete_all_levels(LiquiditySweepReason.STREAM_CHANGED.value)
                )
                self._bars.clear()
                self._stats["stream_resets"] += 1
            if not view.data_gap and not stream_changed:
                duplicate = self._duplicate_status(bar, view)
                if duplicate is not None:
                    return duplicate

            return self._process_bar(view, bar, gap_deletes, stream_deletes, stream_changed)

    def _process_bar(
        self,
        view: MarketView,
        bar: BarSnapshot,
        gap_deletes: list[FeatureDelete],
        stream_deletes: list[FeatureDelete],
        stream_changed: bool,
    ) -> ModuleDecision:
        self._sequence += 1
        restored_features = self._collect_restored_level_features(view)
        features: list[Feature] = list(restored_features)
        deletes: list[FeatureDelete] = [*gap_deletes, *stream_deletes]
        setups: list[SetupEvent] = []
        signals: list[Signal] = []
        skips: list[str] = []
        rejection_counts: dict[str, int] = {}
        geometry = self._evaluator.geometry(bar)
        swept = invalidated = expired = 0

        for level in tuple(self._levels.values()):
            age = level.age(self._sequence)
            if age > self.config.max_level_age_bars:
                deletes.append(self._delete_level(level, LiquiditySweepReason.LEVEL_EXPIRED.value))
                expired += 1
                continue
            measurement = None
            rejection = None
            if age >= self.config.min_level_age_bars:
                measurement, rejection = self._evaluator.evaluate(level, bar, geometry)
            if rejection is not None:
                reason_value = rejection.value
                rejection_counts[reason_value] = rejection_counts.get(reason_value, 0) + 1
            if measurement is not None:
                strength = self._evaluator.signal_strength(measurement)
                event_feature, setup_events, signal = build_sweep_outputs(
                    self.name,
                    level,
                    bar,
                    measurement,
                    age,
                    emit_signal=self.config.emit_signals,
                    signal_strength=strength,
                )
                features.append(event_feature)
                setups.extend(setup_events)
                if signal is not None:
                    signals.append(signal)
                deletes.append(self._delete_level(level, LiquiditySweepReason.LEVEL_SWEPT.value))
                swept += 1
                continue
            if self._evaluator.is_invalidated(level, bar):
                deletes.append(
                    self._delete_level(level, LiquiditySweepReason.ACCEPTED_BREAKOUT.value)
                )
                invalidated += 1

        self._bars.append(bar)
        created_levels = confirm_levels(
            tuple(self._bars),
            self.config,
            self._sequence,
            set(self._levels),
        )
        for level in created_levels:
            self._levels[level.level_id] = level
            features.append(level_feature(self.name, level))
        evicted = self._enforce_level_limit()
        deletes.extend(evicted)
        if view.data_gap:
            skips.append(LiquiditySweepReason.DATA_GAP_RESET.value)
        if stream_changed:
            skips.append(LiquiditySweepReason.STREAM_CHANGED_RESET.value)

        self._last_bar = bar
        self._stats["processed_bars"] += 1
        self._stats["levels_created"] += len(created_levels)
        self._stats["levels_swept"] += swept
        self._stats["levels_invalidated"] += invalidated
        self._stats["levels_expired"] += expired
        self._stats["levels_evicted"] += len(evicted)
        republished = len(restored_features)
        self._stats["rejected_candidates"] += sum(rejection_counts.values())

        return ModuleDecision(
            setups=tuple(setups),
            signals=tuple(signals),
            features=tuple(features),
            feature_deletes=tuple(deletes),
            skips=tuple(skips),
            diagnostics={
                "state": (
                    "active" if len(self._bars) == self.config.pivot_window_bars else "warming_up"
                ),
                "processed_sequence": self._sequence,
                "buffered_bars": len(self._bars),
                "active_levels": len(self._levels),
                "levels_created": len(created_levels),
                "levels_swept": swept,
                "levels_invalidated": invalidated,
                "levels_expired": expired,
                "levels_evicted": len(evicted),
                "levels_republished": republished,
                "stream_changed": stream_changed,
                "rejection_counts": dict(sorted(rejection_counts.items())),
                "totals": dict(self._stats),
                "runtime_restore_count": self._runtime_restore_count,
                "runtime_republished_levels": self._runtime_republished_levels,
            },
        )

    def snapshot_state(self) -> Mapping[str, object]:
        with self._lock:
            return snapshot_state(self)

    def restore_state(self, state: Mapping[str, object]) -> None:
        with self._lock:
            restore_state(self, state)
            self._runtime_restore_count += 1

    def _select_candle(self, view: MarketView) -> Candle | None:
        if self.config.timeframe == "base":
            return view.base_bars[-1] if view.base_bars else None
        return view.current_for(self.config.timeframe)

    @staticmethod
    def _snapshot(candle: Candle) -> BarSnapshot:
        return BarSnapshot(
            timestamp=_utc(candle.timestamp, "candle.timestamp"),
            close_time=_utc(candle.close_time, "candle.close_time"),
            symbol=candle.symbol,
            timeframe=candle.timeframe,
            open=_decimal(candle.open),
            high=_decimal(candle.high),
            low=_decimal(candle.low),
            close=_decimal(candle.close),
        )

    def _duplicate_status(self, bar: BarSnapshot, view: MarketView) -> ModuleDecision | None:
        previous = self._last_bar
        if previous is None:
            return None
        if bar.timestamp < previous.timestamp:
            return ModuleDecision(
                skips=(LiquiditySweepReason.OUT_OF_ORDER_BAR.value,),
                diagnostics={
                    "state": "rejected_bar",
                    "reason": "out_of_order",
                    "bar_time": bar.timestamp.isoformat(),
                    "last_bar_time": previous.timestamp.isoformat(),
                },
            )
        if bar.timestamp == previous.timestamp:
            same_identity = (bar.symbol, bar.timeframe) == (previous.symbol, previous.timeframe)
            same_payload = bar.fingerprint() == previous.fingerprint()
            if same_identity and same_payload:
                restored_features = self._collect_restored_level_features(view)
                diagnostics = {
                    "state": (
                        "waiting_for_new_timeframe_bar"
                        if self.config.timeframe != "base"
                        else "idempotent_skip"
                    ),
                    "reason": "duplicate_bar",
                    "levels_republished": len(restored_features),
                    "runtime_restore_count": self._runtime_restore_count,
                    "runtime_republished_levels": self._runtime_republished_levels,
                }
                return ModuleDecision(
                    features=restored_features,
                    skips=(
                        ()
                        if self.config.timeframe != "base"
                        else (LiquiditySweepReason.DUPLICATE_BAR.value,)
                    ),
                    diagnostics=diagnostics,
                )
            return ModuleDecision(
                skips=(LiquiditySweepReason.CONFLICTING_DUPLICATE_BAR.value,),
                diagnostics={
                    "state": "idempotent_skip",
                    "reason": "conflicting_duplicate_bar",
                },
            )
        return None

    def _delete_level(self, level: LiquidityLevel, reason: str) -> FeatureDelete:
        self._levels.pop(level.level_id, None)
        self._needs_feature_republish.discard(level.level_id)
        return FeatureDelete(
            key=LEVEL_FEATURE_KEY,
            source_module=self.name,
            feature_id=level.feature_id,
            symbol=level.symbol,
            timeframe=level.timeframe,
            reason=reason,
        )

    def _delete_all_levels(self, reason: str) -> tuple[FeatureDelete, ...]:
        return tuple(self._delete_level(level, reason) for level in tuple(self._levels.values()))

    def _enforce_level_limit(self) -> tuple[FeatureDelete, ...]:
        overflow = len(self._levels) - self.config.max_active_levels
        if overflow <= 0:
            return ()
        return tuple(
            self._delete_level(level, LiquiditySweepReason.CAPACITY_EVICTION.value)
            for level in ordered_levels(self._levels)[:overflow]
        )

    def _collect_restored_level_features(self, view: MarketView) -> tuple[Feature, ...]:
        restored: list[Feature] = []
        for level_id in tuple(sorted(self._needs_feature_republish)):
            level = self._levels.get(level_id)
            if level is not None and not self._feature_is_visible(view, level):
                restored.append(level_feature(self.name, level))
            self._needs_feature_republish.discard(level_id)
        self._runtime_republished_levels += len(restored)
        return tuple(restored)

    def _feature_is_visible(self, view: MarketView, level: LiquidityLevel) -> bool:
        feature_view = view.features
        if feature_view is None:
            return False
        return any(
            feature.feature_id == level.feature_id
            for feature in feature_view.get_all(
                LEVEL_FEATURE_KEY,
                symbol=level.symbol,
                timeframe=level.timeframe,
                source_module=self.name,
            )
        )
