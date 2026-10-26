"""Compatibility-preserving hardening layer for Liquidity Sweep 0.1.2."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Mapping

from modular_core import Candle, FeatureDelete, MarketView, ModuleDecision

from .models import BarSnapshot
from .module import LiquiditySweepModule as _LiquiditySweepModuleV011
from .reasons import LiquiditySweepReason
from .state import empty_stats, restore_state


def _decimal(value: object) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("candle OHLC values must be real numbers, not booleans")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("candle OHLC values must be valid decimal numbers") from exc
    if not result.is_finite():
        raise ValueError("candle OHLC values must be finite")
    return result


def _utc(value: datetime, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{field_name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value.astimezone(timezone.utc)


class LiquiditySweepModule(_LiquiditySweepModuleV011):
    """Hardened 0.1.2 adapter without duplicating the detector state machine."""

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
        super().__init__(
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
        self._resume_on_start = False

    def on_start(self) -> None:
        with self._lock:
            if self._resume_on_start:
                self._resume_on_start = False
                return
            self._reset_for_fresh_run()

    def on_finish(self) -> None:
        return None

    def on_bar(self, view: MarketView) -> ModuleDecision:
        with self._lock:
            self._resume_on_start = False
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
                and candle.timeframe.strip().lower() != self.config.timeframe
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
                and (bar.symbol, bar.timeframe) != (self._last_bar.symbol, self._last_bar.timeframe)
            )
            if stream_changed:
                stream_deletes.extend(
                    self._delete_all_levels(LiquiditySweepReason.STREAM_CHANGED.value)
                )
                self._bars.clear()
                self._stats["stream_resets"] += 1
            elif not view.data_gap:
                duplicate = self._duplicate_status(bar, view)
                if duplicate is not None:
                    return duplicate

            return self._process_bar(
                view,
                bar,
                gap_deletes,
                stream_deletes,
                stream_changed,
            )

    def restore_state(self, state: Mapping[str, object]) -> None:
        with self._lock:
            restore_state(self, state)
            self._runtime_restore_count += 1
            self._resume_on_start = True

    @staticmethod
    def _snapshot(candle: Candle) -> BarSnapshot:
        timeframe = candle.timeframe.strip().lower()
        try:
            close_time = candle.close_time
        except OverflowError as exc:
            raise ValueError("candle timeframe duration is out of supported range") from exc
        return BarSnapshot(
            timestamp=_utc(candle.timestamp, "candle.timestamp"),
            close_time=_utc(close_time, "candle.close_time"),
            symbol=candle.symbol,
            timeframe=timeframe,
            open=_decimal(candle.open),
            high=_decimal(candle.high),
            low=_decimal(candle.low),
            close=_decimal(candle.close),
        )

    def _reset_for_fresh_run(self) -> None:
        self._bars.clear()
        self._levels.clear()
        self._sequence = 0
        self._last_bar = None
        self._stats = empty_stats()
        self._needs_feature_republish.clear()
        self._runtime_restore_count = 0
        self._runtime_republished_levels = 0
