"""Completed-bar runtime producer for the owner-bound FVG-RT Final 15m contract.

This module intentionally owns only signal construction.  It publishes a
strict, immutable snapshot for the canonical setup adapter; it never creates
orders, sizes positions, or mutates execution state.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from types import MappingProxyType
from collections.abc import Sequence
from typing import Iterable, Mapping

from modular_core import (
    Candle,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    ReadOnlySeries,
    StrategyModule,
)

FEATURE_KEY = "fvg_rt_final_15m.snapshot"
MODULE_NAME = "fvg_rt_final_15m"
MODULE_VERSION = "2.0.0"
FEATURE_CONTRACT = "canonical-fvg-final-15m-feature-snapshot-v1"
_REQUIRED_TIMEFRAMES = ("15m", "4h", "1d")
_COMPLETED_TIMEFRAMES_BY_SETUP: Mapping[str, tuple[str, ...]] = MappingProxyType(
    {
        "long": ("5m", "15m", "4h"),
        "short_a": ("5m", "15m", "4h", "1d"),
        "short_b": ("5m", "15m", "1d"),
    }
)
_VALIDITY_WINDOW = timedelta(microseconds=1)


@dataclass(frozen=True, slots=True)
class _ReadOnlySeriesGeometry:
    source: Sequence[Candle]
    start: int
    end: int


def _read_only_series_geometry(
    series: Sequence[Candle],
) -> _ReadOnlySeriesGeometry | None:
    """Return trusted append-only snapshot geometry, or require a full scan.

    MarketView's engine-only snapshot path supplies exact ReadOnlySeries objects.
    Their private representation is inspected defensively: if the class or its
    append-only geometry changes, this helper returns None and callers retain
    the full fail-closed validation path.
    """

    if type(series) is not ReadOnlySeries:
        return None
    source = getattr(series, "_ReadOnlySeries__source", None)
    start = getattr(series, "_ReadOnlySeries__start", None)
    end = getattr(series, "_ReadOnlySeries__end", None)
    if (
        not isinstance(source, Sequence)
        or not isinstance(start, int)
        or isinstance(start, bool)
        or not isinstance(end, int)
        or isinstance(end, bool)
        or not 0 <= start <= end <= len(source)
    ):
        return None
    return _ReadOnlySeriesGeometry(source=source, start=start, end=end)


class _SymbolSeriesValidator:
    """Validate every visible candle while rechecking only proven new bars."""

    def __init__(self) -> None:
        self._series: dict[str, _ReadOnlySeriesGeometry] = {}
        self._view_index: int | None = None

    def reset(self) -> None:
        self._series.clear()
        self._view_index = None

    def matches(
        self,
        series_by_timeframe: Mapping[str, Sequence[Candle]],
        required_timeframes: Sequence[str],
        expected_symbol: str,
        *,
        view_index: int,
    ) -> bool:
        names = tuple(required_timeframes)
        geometry = {
            timeframe: _read_only_series_geometry(series_by_timeframe[timeframe])
            for timeframe in names
        }
        trusted = all(value is not None for value in geometry.values())
        can_increment = (
            trusted
            and self._view_index is not None
            and view_index > self._view_index
            and self._series.keys() == geometry.keys()
        )

        if can_increment:
            for timeframe, current in geometry.items():
                previous = self._series[timeframe]
                if current is None or current.source is not previous.source:
                    can_increment = False
                    break
                start_delta = current.start - previous.start
                end_delta = current.end - previous.end
                if (start_delta, end_delta) == (0, 0):
                    continue
                if (start_delta, end_delta) not in {(0, 1), (1, 1)}:
                    can_increment = False
                    break
                if current.end <= current.start:
                    can_increment = False
                    break
                if current.source[current.end - 1].symbol != expected_symbol:
                    self.reset()
                    return False

        if can_increment:
            self._series = {
                timeframe: value for timeframe, value in geometry.items() if value is not None
            }
            self._view_index = view_index
            return True

        for timeframe in names:
            if any(candle.symbol != expected_symbol for candle in series_by_timeframe[timeframe]):
                self.reset()
                return False

        if trusted:
            self._series = {
                timeframe: value for timeframe, value in geometry.items() if value is not None
            }
            self._view_index = view_index
        else:
            self.reset()
        return True


CONDITIONS_BY_SETUP: Mapping[str, frozenset[str]] = MappingProxyType(
    {
        "long": frozenset(
            {
                "all_inputs_completed",
                "atr_fraction_inclusive",
                "bullish_fvg",
                "close_above_ema50",
                "completed_4h_close_above_ema200",
                "ema50_rising_4",
                "fvg_size_atr_inclusive",
                "impulse_efficiency_min",
                "location_12h_min",
                "location_8h_min",
                "middle_body_atr_min",
                "middle_bullish",
                "rsi14_min",
                "session_allowed",
                "volume_ratio_min",
            }
        ),
        "short_a": frozenset(
            {
                "all_inputs_completed",
                "atr_fraction_inclusive",
                "bearish_fvg",
                "completed_1d_close_below_ema200",
                "completed_4h_trend_down_3",
                "fvg_size_atr_inclusive",
                "impulse_efficiency_min",
                "middle_bearish",
                "middle_body_atr_min",
                "short_location_4h_max",
                "volume_ratio_min",
            }
        ),
        "short_b": frozenset(
            {
                "all_inputs_completed",
                "atr_fraction_inclusive",
                "bearish_bos_48",
                "bearish_fvg",
                "close_strength_min",
                "completed_1d_close_below_ema200",
                "fvg_size_atr_inclusive",
                "impulse_efficiency_min",
                "middle_bearish",
                "middle_body_atr_min",
                "no_recent_bullish_fvg_12",
                "rsi14_max",
                "short_location_48h_min",
                "short_location_8h_min",
                "volume_ratio_min",
            }
        ),
    }
)

_SETUP_PARAMETERS: Mapping[str, tuple[str, float, int, int, int, float]] = MappingProxyType(
    {
        "long": ("buy", 4.5, 36, 288, 48, 0.15),
        "short_a": ("sell", 5.0, 48, 288, 24, 0.35),
        "short_b": ("sell", 6.0, 48, 576, 60, 0.35),
    }
)


@dataclass(frozen=True, slots=True)
class _Indicators:
    atr14: float
    ema50: float
    ema200: float
    rsi14: float
    volume_sma20: float


class _IndicatorCache:
    """Incrementally reproduce the exact historical indicator recurrence."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._first: Candle | None = None
        self._last: Candle | None = None
        self._true_ranges: list[float] = []
        self._results: list[_Indicators] = []
        self._ema50 = math.nan
        self._ema200 = math.nan
        self._average_gain = math.nan
        self._average_loss = math.nan

    def values(self, candles: Sequence[Candle]) -> Sequence[_Indicators]:
        length = len(candles)
        cached = len(self._results)
        prefix_matches = cached == 0 or (
            length >= cached and candles[0] is self._first and candles[cached - 1] is self._last
        )
        if not prefix_matches:
            # Capped timeframe windows shift their visible prefix. Materialize only
            # this forced-recompute path so the exact recurrence below no longer
            # pays Python-level ReadOnlySeries.__getitem__ cost for every lookup.
            candles = tuple(candles)
            self.reset()
            cached = 0
        if length == 0:
            self.reset()
            return self._results
        if cached == 0:
            self._first = candles[0]

        for index in range(cached, length):
            candle = candles[index]
            previous_close = candles[index - 1].close if index else math.nan
            true_range = max(
                candle.high - candle.low,
                abs(candle.high - previous_close) if index else candle.high - candle.low,
                abs(candle.low - previous_close) if index else candle.high - candle.low,
            )
            self._true_ranges.append(true_range)
            atr14 = sum(self._true_ranges[index - 13 : index + 1]) / 14 if index >= 13 else math.nan
            if index == 0:
                self._ema50 = self._ema200 = candle.close
            else:
                self._ema50 = (candle.close - self._ema50) * (2 / 51) + self._ema50
                self._ema200 = (candle.close - self._ema200) * (2 / 201) + self._ema200
            shown_ema50 = self._ema50 if index >= 49 else math.nan
            shown_ema200 = self._ema200 if index >= 199 else math.nan

            if index == 0:
                gain = loss = math.nan
            else:
                delta = candle.close - previous_close
                gain, loss = max(delta, 0.0), max(-delta, 0.0)
            if index == 1:
                self._average_gain, self._average_loss = gain, loss
            elif index > 1:
                self._average_gain = gain / 14 + self._average_gain * (13 / 14)
                self._average_loss = loss / 14 + self._average_loss * (13 / 14)

            rsi14 = math.nan
            if index >= 14 and _finite(self._average_gain) and _finite(self._average_loss):
                if self._average_loss == 0:
                    rsi14 = 100.0 if self._average_gain > 0 else 50.0
                else:
                    rsi14 = 100.0 - 100.0 / (1.0 + self._average_gain / self._average_loss)
            volume_sma20 = (
                sum(candles[position].volume for position in range(index - 19, index + 1)) / 20
                if index >= 19
                else math.nan
            )
            self._results.append(_Indicators(atr14, shown_ema50, shown_ema200, rsi14, volume_sma20))
            self._last = candle
        return self._results


class FvgRtFinal15mModule(StrategyModule):
    """Reproduce the canonical FVG-RT Final 15m signal contract on closed bars."""

    name = MODULE_NAME

    def __init__(self) -> None:
        self.parameters: Mapping[str, object] = MappingProxyType(
            {
                "contract": FEATURE_CONTRACT,
                "module_version": MODULE_VERSION,
                "required_timeframes": _REQUIRED_TIMEFRAMES,
            }
        )
        self.manifest = ModuleManifest(
            name=MODULE_NAME,
            version=MODULE_VERSION,
            api_version="1.0",
            required_timeframes=_REQUIRED_TIMEFRAMES,
            produced_features=(FEATURE_KEY,),
            warmup_bars={"15m": 200, "4h": 200, "1d": 200},
            parameters_schema={
                "contract": {"type": "string", "default": FEATURE_CONTRACT},
                "module_version": {"type": "string", "default": MODULE_VERSION},
                "required_timeframes": {"type": "array", "default": _REQUIRED_TIMEFRAMES},
            },
            skip_until_ready=False,
            description=(
                "Owner-bound FVG-RT Final 15m completed-bar signal producer; "
                "publishes validated snapshots only."
            ),
            state_schema_version="1",
        )
        self._last_processed_close: datetime | None = None
        self._bound_symbol: str | None = None
        self._indicator_cache = {timeframe: _IndicatorCache() for timeframe in _REQUIRED_TIMEFRAMES}
        self._symbol_series_validator = _SymbolSeriesValidator()

    def on_bar(self, view: MarketView) -> ModuleDecision:
        current = view.current
        if current.timeframe != "5m":
            return ModuleDecision(skips=("fvg_rt_final_15m.base_timeframe_mismatch",))
        if view.data_gap:
            self._last_processed_close = None
            self._bound_symbol = None
            self._symbol_series_validator.reset()
            for cache in self._indicator_cache.values():
                cache.reset()
            return ModuleDecision(skips=("fvg_rt_final_15m.data_gap_reset",))
        if self._bound_symbol is None:
            self._bound_symbol = current.symbol
        elif self._bound_symbol != current.symbol:
            return ModuleDecision(skips=("fvg_rt_final_15m.symbol_mismatch",))

        current_15m = view.current_for("15m")
        if current_15m is None:
            return ModuleDecision(skips=("fvg_rt_final_15m.waiting_for_15m",))
        if current_15m.symbol != current.symbol or current_15m.close_time > current.close_time:
            return ModuleDecision(skips=("fvg_rt_final_15m.incomplete_context",))
        if current_15m.close_time != current.close_time:
            return ModuleDecision(skips=("fvg_rt_final_15m.waiting_for_15m_close",))
        if self._last_processed_close == current_15m.close_time:
            return ModuleDecision(skips=("fvg_rt_final_15m.duplicate_15m_close",))

        series = {
            timeframe: view.timeframe_bars.get(timeframe, ()) for timeframe in _REQUIRED_TIMEFRAMES
        }
        if any(not series[timeframe] for timeframe in _REQUIRED_TIMEFRAMES):
            return ModuleDecision(skips=("fvg_rt_final_15m.incomplete_context",))
        if not self._symbol_series_validator.matches(
            series,
            _REQUIRED_TIMEFRAMES,
            current.symbol,
            view_index=view.index,
        ):
            return ModuleDecision(skips=("fvg_rt_final_15m.symbol_mismatch",))

        self._last_processed_close = current_15m.close_time
        snapshots = self._snapshots(
            bars_15m=series["15m"],
            bars_4h=series["4h"],
            bars_1d=series["1d"],
            available_at=current.close_time,
            symbol=current.symbol,
        )
        features = tuple(
            self._feature(snapshot, current.close_time, current.symbol) for snapshot in snapshots
        )
        return ModuleDecision(
            features=features,
            diagnostics={
                "contract": FEATURE_CONTRACT,
                "processed_15m_close": current_15m.close_time.isoformat(),
                "snapshots_published": len(features),
            },
        )

    def snapshot_state(self) -> Mapping[str, object]:
        """Persist the minimal stream identity needed for deterministic replay."""

        return {
            "payload_version": 1,
            "last_processed_close": (
                self._last_processed_close.astimezone(UTC).isoformat()
                if self._last_processed_close is not None
                else None
            ),
            "bound_symbol": self._bound_symbol,
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        """Restore a state produced by :meth:`snapshot_state` and fail closed."""

        expected = {"payload_version", "last_processed_close", "bound_symbol"}
        if set(state) != expected:
            raise ValueError("invalid FVG-RT state keys")
        version = state["payload_version"]
        if isinstance(version, bool) or version != 1:
            raise ValueError("unsupported FVG-RT state payload version")

        close_raw = state["last_processed_close"]
        if close_raw is None:
            last_processed_close = None
        elif isinstance(close_raw, str):
            try:
                parsed = datetime.fromisoformat(close_raw)
            except ValueError as exc:
                raise ValueError("invalid FVG-RT last_processed_close") from exc
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("FVG-RT last_processed_close must be timezone-aware")
            last_processed_close = parsed.astimezone(UTC)
        else:
            raise ValueError("invalid FVG-RT last_processed_close type")

        symbol_raw = state["bound_symbol"]
        if symbol_raw is None:
            bound_symbol = None
        elif isinstance(symbol_raw, str) and symbol_raw.strip():
            bound_symbol = symbol_raw
        else:
            raise ValueError("invalid FVG-RT bound_symbol")

        if last_processed_close is not None and bound_symbol is None:
            raise ValueError("processed FVG-RT state requires bound_symbol")

        self._last_processed_close = last_processed_close
        self._bound_symbol = bound_symbol
        self._symbol_series_validator.reset()
        for cache in self._indicator_cache.values():
            cache.reset()

    def _snapshots(
        self,
        *,
        bars_15m: Sequence[Candle],
        bars_4h: Sequence[Candle],
        bars_1d: Sequence[Candle],
        available_at: datetime,
        symbol: str,
    ) -> tuple[Mapping[str, object], ...]:
        index = len(bars_15m) - 1
        if index < 2:
            return ()
        row = bars_15m[index]
        a, b = bars_15m[index - 2], bars_15m[index - 1]
        bullish = row.low > a.high
        bearish = row.high < a.low
        if not bullish and not bearish:
            return ()

        denominator = _range(a) + _range(b) + _range(row)
        if denominator <= 0:
            return ()

        indicators = self._indicator_cache["15m"].values(bars_15m)
        if index >= len(indicators):
            return ()
        current = indicators[index]
        if not _positive_finite(current.atr14) or not _finite(current.volume_sma20):
            return ()

        atr = current.atr14
        body_atr = abs(b.close - b.open) / atr
        previous = indicators[index - 1]
        volume_ratio = b.volume / previous.volume_sma20 if previous.volume_sma20 > 0 else math.nan
        atr_fraction = atr / row.close
        quality = abs(row.close - a.open) / denominator
        four = _completed_asof(bars_4h, available_at)
        four_index = (
            len(bars_4h) - 1
            if four is not None and bars_4h and bars_4h[-1] is four
            else bars_4h.index(four)
            if four is not None
            else -1
        )

        result: list[Mapping[str, object]] = []
        if bullish:
            four_values = None
            if four_index >= 0:
                four_indicators = self._indicator_cache["4h"].values(bars_4h)
                if four_index < len(four_indicators):
                    four_values = four_indicators[four_index]
            fvg_size = (row.low - a.high) / atr
            location_8h = _rolling_location(bars_15m, 32, short=False)
            location_12h = _rolling_location(bars_15m, 48, short=False)
            ema_rising = index >= 4 and current.ema50 > indicators[index - 4].ema50
            if (
                0.10 <= fvg_size <= 0.50
                and body_atr >= 0.75
                and b.close > b.open
                and volume_ratio >= 1.0
                and row.close > current.ema50
                and ema_rising
                and location_8h >= 0.25
                and location_12h >= 0.25
                and current.rsi14 >= 60
                and 0.002 <= atr_fraction <= 0.012
                and not 0 <= available_at.hour <= 5
                and quality >= 0.45
                and four is not None
                and four_values is not None
                and _finite(four_values.ema200)
                and four.close > four_values.ema200
            ):
                entry = row.low
                stop = (
                    min(candle.low for candle in bars_15m[max(0, index - 15) : index + 1])
                    - 0.20 * atr
                )
                snapshot = self._snapshot("long", symbol, available_at, entry, stop)
                if snapshot is not None:
                    result.append(snapshot)
        if bearish:
            daily = _completed_asof(bars_1d, available_at)
            daily_values = None
            if daily is not None:
                daily_index = (
                    len(bars_1d) - 1 if bars_1d and bars_1d[-1] is daily else bars_1d.index(daily)
                )
                daily_indicators = self._indicator_cache["1d"].values(bars_1d)
                if daily_index < len(daily_indicators):
                    daily_values = daily_indicators[daily_index]
            fvg_size = (a.low - row.high) / atr
            trend_down_3 = (
                four is not None and four_index >= 3 and four.close < bars_4h[four_index - 3].close
            )
            short_location_4h = (
                _single_candle_short_location(four) if four is not None else math.nan
            )
            daily_below_ema200 = (
                daily is not None
                and daily_values is not None
                and _finite(daily_values.ema200)
                and daily.close < daily_values.ema200
            )
            if (
                0.15 <= fvg_size <= 1.50
                and body_atr >= 0.25
                and b.close < b.open
                and volume_ratio >= 0.70
                and trend_down_3
                and short_location_4h <= 0.60
                and 0.002 <= atr_fraction <= 0.005
                and quality >= 0.45
                and daily_below_ema200
            ):
                entry = row.high + 0.25 * (a.low - row.high)
                stop = max(candle.high for candle in bars_15m[max(0, index - 7) : index + 1])
                snapshot = self._snapshot("short_a", symbol, available_at, entry, stop)
                if snapshot is not None:
                    result.append(snapshot)
            close_strength = (b.high - b.close) / _range(b) if _range(b) > 0 else math.nan
            previous_48_low = (
                min(candle.low for candle in bars_15m[index - 48 : index])
                if index >= 48
                else math.nan
            )
            recent_bullish = any(
                bars_15m[position].low > bars_15m[position - 2].high
                for position in range(max(2, index - 12), index)
            )
            location_8h_short = _rolling_location(bars_15m, 32, short=True)
            location_48h_short = _rolling_location(bars_15m, 192, short=True)
            if (
                0.20 <= fvg_size <= 2.00
                and body_atr >= 0.50
                and b.close < b.open
                and volume_ratio >= 0.70
                and row.close < previous_48_low
                and current.rsi14 <= 40
                and close_strength >= 0.50
                and location_8h_short >= 0.50
                and location_48h_short >= 0.60
                and not recent_bullish
                and 0 <= atr_fraction <= 0.02
                and quality >= 0.45
                and daily_below_ema200
            ):
                entry = row.high
                stop = max(candle.high for candle in bars_15m[max(0, index - 15) : index + 1])
                snapshot = self._snapshot("short_b", symbol, available_at, entry, stop)
                if snapshot is not None:
                    result.append(snapshot)
        return tuple(result)

    def _snapshot(
        self,
        setup_id: str,
        symbol: str,
        available_at: datetime,
        entry: float,
        stop: float,
    ) -> Mapping[str, object] | None:
        side, target_r, ttl_bars, max_hold, no_progress, minimum_mfe = _SETUP_PARAMETERS[setup_id]
        distance = entry - stop if side == "buy" else stop - entry
        maximum_fraction = {"long": 0.05, "short_a": 0.015, "short_b": 0.03}[setup_id]
        if not (_positive_finite(entry) and _positive_finite(stop)) or distance <= 0:
            return None
        if distance / entry > maximum_fraction:
            return None
        conditions = {name: True for name in sorted(CONDITIONS_BY_SETUP[setup_id])}
        completed_timeframes = _COMPLETED_TIMEFRAMES_BY_SETUP[setup_id]
        identity = {
            "available_at": available_at.astimezone(UTC).isoformat(),
            "entry_price": _number_text(entry),
            "module_id": MODULE_NAME,
            "module_version": MODULE_VERSION,
            "setup_id": setup_id,
            "stop_price": _number_text(stop),
            "symbol": symbol,
        }
        snapshot_id = _sha256(identity)
        semantic_input_digest = _sha256(
            {
                "available_at": available_at.astimezone(UTC).isoformat(),
                "entry": round(entry, 12),
                "module_id": MODULE_NAME,
                "module_version": MODULE_VERSION,
                "setup_id": setup_id,
                "stop": round(stop, 12),
            }
        )
        return MappingProxyType(
            {
                "snapshot_id": snapshot_id,
                "module_id": MODULE_NAME,
                "module_version": MODULE_VERSION,
                "setup_id": setup_id,
                "symbol": symbol,
                "side": side,
                "signal_available_at": available_at.astimezone(UTC).isoformat(),
                "first_eligible_5m_open": available_at.astimezone(UTC).isoformat(),
                "completed_timeframes": completed_timeframes,
                "conditions": conditions,
                "entry_price": _number_text(entry),
                "stop_price": _number_text(stop),
                "target_r": _number_text(target_r),
                "ttl_bars_5m": ttl_bars,
                "max_hold_bars_5m": max_hold,
                "no_progress_bars_5m": no_progress,
                "no_progress_min_mfe_r": _number_text(minimum_mfe),
                "semantic_input_digest": semantic_input_digest,
                "feature_contract": FEATURE_CONTRACT,
            }
        )

    @staticmethod
    def _feature(snapshot: Mapping[str, object], created_at: datetime, symbol: str) -> Feature:
        snapshot_id = str(snapshot["snapshot_id"])
        return Feature(
            key=FEATURE_KEY,
            source_module=MODULE_NAME,
            created_at=created_at,
            valid_until=created_at + _VALIDITY_WINDOW,
            value=dict(snapshot),
            symbol=symbol,
            timeframe="15m",
            schema_version="1",
            feature_id=f"fvg-rt-{snapshot_id}",
            invalidate_on_gap=True,
            metadata={"feature_contract": FEATURE_CONTRACT},
        )


def _indicators(candles: Iterable[Candle]) -> tuple[_Indicators, ...]:
    bars = tuple(candles)
    return tuple(_IndicatorCache().values(bars))


def _completed_asof(candles: Sequence[Candle], available_at: datetime) -> Candle | None:
    return next((candle for candle in reversed(candles) if candle.close_time <= available_at), None)


def _rolling_location(candles: Sequence[Candle], window: int, *, short: bool) -> float:
    if len(candles) < window:
        return math.nan
    sample = candles[-window:]
    high, low = max(candle.high for candle in sample), min(candle.low for candle in sample)
    if high <= low:
        return math.nan
    return (high - candles[-1].close if short else candles[-1].close - low) / (high - low)


def _single_candle_short_location(candle: Candle) -> float:
    candle_range = _range(candle)
    return (candle.high - candle.close) / candle_range if candle_range > 0 else math.nan


def _range(candle: Candle) -> float:
    return candle.high - candle.low


def _finite(value: float) -> bool:
    return isinstance(value, float) and math.isfinite(value)


def _positive_finite(value: float) -> bool:
    return _finite(value) and value > 0


def _number_text(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("snapshot values must be finite")
    return format(value, ".17g")


def _sha256(value: Mapping[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(value, allow_nan=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
