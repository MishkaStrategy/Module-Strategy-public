from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Mapping

from modular_core import Candle, Feature, MarketView, ModuleDecision, StrategyModule

from .config import (
    MarketRegimeConfig,
    canonical_timeframe,
    to_decimal,
)
from .contracts import (
    CURRENT_FEATURE_KEY,
    FEATURE_SCHEMA_VERSION,
    TREND_FEATURE_KEY,
    VOLATILITY_FEATURE_KEY,
    CurrentRegimeValue,
    IndicatorMetricsValue,
    TrendFeatureValue,
    VolatilityFeatureValue,
)
from .enums import Regime, SkipReason, VolatilityLevel
from .exceptions import (
    MarketRegimeConfigurationError,
    MarketRegimeDataError,
)
from .indicators import IndicatorMetrics, build_constants, update_indicators
from .manifest import MODULE_NAME, MODULE_VERSION, build_manifest
from .state import RuntimeState


def _candle_timeframe(candle: Candle) -> str:
    try:
        return canonical_timeframe(candle.timeframe)
    except MarketRegimeConfigurationError as exc:
        raise MarketRegimeDataError(
            f"candle timeframe {candle.timeframe!r} is not canonicalizable"
        ) from exc


def _fingerprint(candle: Candle, timeframe: str) -> str:
    """Stable exact identity for duplicate and checkpoint validation."""
    return "|".join(
        (
            candle.timestamp.isoformat(),
            candle.symbol,
            timeframe,
            float(candle.open).hex(),
            float(candle.high).hex(),
            float(candle.low).hex(),
            float(candle.close).hex(),
            float(candle.volume).hex(),
        )
    )


def _float(value: Decimal) -> float:
    return float(value)


class MarketRegimeModule(StrategyModule):
    """Deterministic feature producer for trend/range and volatility regimes.

    Only completed candles supplied by ``MarketView`` are consumed. The module
    never creates orders, signals, setups, portfolio mutations or broker calls.
    One instance is permanently bound to one symbol. Its base timeframe may
    change only across an explicit, chronologically new data-gap barrier.
    """

    name = MODULE_NAME

    def __init__(
        self,
        *,
        timeframe: str = "1h",
        fast_ema_period: int = 20,
        slow_ema_period: int = 50,
        atr_period: int = 14,
        volatility_baseline_period: int = 50,
        efficiency_period: int = 20,
        slope_lookback: int = 5,
        trend_spread_threshold: int | float | str | Decimal = 0.001,
        trend_slope_threshold: int | float | str | Decimal = 0.00015,
        trend_efficiency_threshold: int | float | str | Decimal = 0.35,
        range_efficiency_threshold: int | float | str | Decimal = 0.22,
        low_volatility_ratio: int | float | str | Decimal = 0.75,
        high_volatility_ratio: int | float | str | Decimal = 1.35,
        regime_confirmation_bars: int = 2,
        volatility_confirmation_bars: int = 2,
        publish_diagnostics: bool = True,
    ) -> None:
        self.config = MarketRegimeConfig(
            timeframe=timeframe,
            fast_ema_period=fast_ema_period,
            slow_ema_period=slow_ema_period,
            atr_period=atr_period,
            volatility_baseline_period=volatility_baseline_period,
            efficiency_period=efficiency_period,
            slope_lookback=slope_lookback,
            trend_spread_threshold=to_decimal(trend_spread_threshold, "trend_spread_threshold"),
            trend_slope_threshold=to_decimal(trend_slope_threshold, "trend_slope_threshold"),
            trend_efficiency_threshold=to_decimal(
                trend_efficiency_threshold,
                "trend_efficiency_threshold",
            ),
            range_efficiency_threshold=to_decimal(
                range_efficiency_threshold,
                "range_efficiency_threshold",
            ),
            low_volatility_ratio=to_decimal(low_volatility_ratio, "low_volatility_ratio"),
            high_volatility_ratio=to_decimal(high_volatility_ratio, "high_volatility_ratio"),
            regime_confirmation_bars=regime_confirmation_bars,
            volatility_confirmation_bars=volatility_confirmation_bars,
            publish_diagnostics=publish_diagnostics,
        )
        self.parameters = self.config.as_parameters()
        self.manifest = build_manifest(self.config)
        self._constants = build_constants(self.config)
        self._target_delta = timedelta(seconds=self.config.target_seconds)
        self._state = RuntimeState.empty(self.config)

    def on_start(self) -> None:
        self._state = RuntimeState.empty(self.config)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not view.base_bars:
            return self._skip(SkipReason.EMPTY_VIEW)

        base_bar = view.current
        self._bind_symbol(base_bar)
        base_timeframe = _candle_timeframe(base_bar)
        base_fingerprint = _fingerprint(base_bar, base_timeframe)

        if view.data_gap:
            if self._validate_base_ordering(base_bar, base_fingerprint):
                return self._skip(SkipReason.DUPLICATE_BAR)
            self._reset_runtime(
                data_gap=True,
                base_timeframe=base_timeframe,
            )
            self._remember_base(base_bar, base_fingerprint)
            return self._skip(SkipReason.DATA_GAP_RESET)

        self._bind_base_stream_canonical(base_timeframe)
        if self._validate_base_ordering(base_bar, base_fingerprint):
            return self._skip(SkipReason.DUPLICATE_BAR)

        target_bar = (
            base_bar if base_timeframe == self.config.timeframe else self._current_target(view)
        )
        if target_bar is None:
            self._remember_base(base_bar, base_fingerprint)
            return self._skip(SkipReason.NO_TARGET_BAR)

        target_timeframe = _candle_timeframe(target_bar)
        if target_timeframe != self.config.timeframe:
            raise MarketRegimeDataError(
                f"target candle timeframe {target_bar.timeframe!r} does not "
                f"match configured timeframe {self.config.timeframe!r}"
            )
        if target_bar.symbol != base_bar.symbol:
            raise MarketRegimeDataError(
                f"target candle symbol {target_bar.symbol!r} does not match "
                f"base symbol {base_bar.symbol!r}"
            )
        if target_bar.close_time > base_bar.close_time:
            self._remember_base(base_bar, base_fingerprint)
            return self._skip(
                SkipReason.INCOMPLETE_BAR,
                target_close=target_bar.close_time.isoformat(),
                decision_time=base_bar.close_time.isoformat(),
            )

        target_fingerprint = (
            base_fingerprint
            if target_bar is base_bar
            else _fingerprint(target_bar, target_timeframe)
        )
        last_target_open = self._state.last_open_time
        if last_target_open is not None:
            if target_bar.timestamp < last_target_open:
                raise MarketRegimeDataError(
                    f"out-of-order {self.config.timeframe} bar: "
                    f"{target_bar.timestamp.isoformat()} after "
                    f"{last_target_open.isoformat()}"
                )
            if target_bar.timestamp == last_target_open:
                if target_fingerprint != self._state.last_fingerprint:
                    raise MarketRegimeDataError(
                        "conflicting duplicate target candle for the already processed timestamp"
                    )
                self._remember_base(base_bar, base_fingerprint)
                return self._skip(SkipReason.STALE_TARGET)

        self._remember_base(base_bar, base_fingerprint)
        target_gap_reset = False
        if last_target_open is not None:
            if target_bar.timestamp - last_target_open != self._target_delta:
                self._reset_runtime(timeframe_gap=True)
                self._remember_base(base_bar, base_fingerprint)
                target_gap_reset = True

        update = update_indicators(
            self._state,
            target_bar,
            self.config,
            self._constants,
        )
        self._state.last_open_time = target_bar.timestamp
        self._state.last_fingerprint = target_fingerprint

        if target_gap_reset:
            return self._skip(SkipReason.TIMEFRAME_GAP_RESET)
        if update.metrics is None:
            return self._skip(
                SkipReason.MODULE_NOT_READY,
                processed_bars=self._state.processed_bars,
                required_bars=self.config.minimum_observations,
                missing=update.missing,
            )

        self._update_regime_hysteresis(update.metrics.raw_regime)
        self._update_volatility_hysteresis(update.metrics.raw_volatility)
        if (
            self._state.stable_regime is Regime.UNKNOWN
            or self._state.stable_volatility is VolatilityLevel.UNKNOWN
        ):
            return self._skip(
                SkipReason.MODULE_NOT_READY,
                processed_bars=self._state.processed_bars,
                required_bars=self.config.minimum_observations,
                missing=("confirmation_warmup",),
            )

        self._state.publications += 1
        return self._decision(target_bar, update.metrics)

    def snapshot_state(self) -> Mapping[str, object]:
        return dict(self._state.to_mapping(self.config))

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise TypeError("market_regime state must be a mapping")
        restored = RuntimeState.from_mapping(state, self.config)
        self._state = restored

    def _current_target(self, view: MarketView) -> Candle | None:
        candidate = view.current_for(self.config.timeframe)
        for key, series in view.timeframe_bars.items():
            try:
                normalized = canonical_timeframe(key)
            except MarketRegimeConfigurationError:
                continue
            if normalized != self.config.timeframe or not series:
                continue
            alias_candidate = series[-1]
            if candidate is not None and alias_candidate != candidate:
                raise MarketRegimeDataError(
                    "conflicting target histories use equivalent timeframe aliases"
                )
            candidate = alias_candidate
        return candidate

    def _validate_base_ordering(
        self,
        candle: Candle,
        fingerprint: str,
    ) -> bool:
        last_open = self._state.last_base_open_time
        if last_open is None:
            return False
        if candle.timestamp < last_open:
            raise MarketRegimeDataError(
                f"out-of-order base bar: {candle.timestamp.isoformat()} after "
                f"{last_open.isoformat()}"
            )
        if candle.timestamp == last_open:
            if fingerprint != self._state.last_base_fingerprint:
                raise MarketRegimeDataError(
                    "conflicting duplicate base candle for the already processed timestamp"
                )
            self._state.duplicate_events += 1
            return True
        return False

    def _reset_runtime(
        self,
        *,
        data_gap: bool = False,
        timeframe_gap: bool = False,
        base_timeframe: str | None = None,
    ) -> None:
        previous = self._state
        state = RuntimeState.empty(
            self.config,
            symbol=previous.bound_symbol,
            base_timeframe=(
                previous.bound_base_timeframe if base_timeframe is None else base_timeframe
            ),
        )
        state.data_gap_resets = previous.data_gap_resets + int(data_gap)
        state.timeframe_gap_resets = previous.timeframe_gap_resets + int(timeframe_gap)
        state.duplicate_events = previous.duplicate_events
        state.publications = previous.publications
        self._state = state

    def _health(self) -> dict[str, int]:
        return {
            "data_gap_resets": self._state.data_gap_resets,
            "timeframe_gap_resets": self._state.timeframe_gap_resets,
            "duplicate_events": self._state.duplicate_events,
            "publications": self._state.publications,
        }

    def _remember_base(self, candle: Candle, fingerprint: str) -> None:
        self._state.last_base_open_time = candle.timestamp
        self._state.last_base_fingerprint = fingerprint

    def _bind_base_stream_canonical(self, timeframe: str) -> None:
        if self._state.bound_base_timeframe is None:
            self._state.bound_base_timeframe = timeframe
            return
        if timeframe != self._state.bound_base_timeframe:
            raise MarketRegimeDataError(
                f"module instance is bound to base timeframe "
                f"{self._state.bound_base_timeframe}, got {timeframe}; use a "
                "chronologically new data-gap reset barrier or a new instance"
            )

    def _bind_symbol(self, candle: Candle) -> None:
        if self._state.bound_symbol is None:
            self._state.bound_symbol = candle.symbol
            return
        if candle.symbol != self._state.bound_symbol:
            raise MarketRegimeDataError(
                f"module instance is bound to {self._state.bound_symbol}, got "
                f"{candle.symbol}; use one MarketRegimeModule instance per "
                "instrument"
            )

    def _update_regime_hysteresis(self, raw: Regime) -> None:
        state = self._state
        if raw is state.stable_regime:
            state.candidate_regime = Regime.UNKNOWN
            state.candidate_regime_count = 0
            return
        if raw is state.candidate_regime:
            state.candidate_regime_count += 1
        else:
            state.candidate_regime = raw
            state.candidate_regime_count = 1
        if state.candidate_regime_count >= self.config.regime_confirmation_bars:
            state.stable_regime = raw
            state.candidate_regime = Regime.UNKNOWN
            state.candidate_regime_count = 0

    def _update_volatility_hysteresis(
        self,
        raw: VolatilityLevel,
    ) -> None:
        state = self._state
        if raw is state.stable_volatility:
            state.candidate_volatility = VolatilityLevel.UNKNOWN
            state.candidate_volatility_count = 0
            return
        if raw is state.candidate_volatility:
            state.candidate_volatility_count += 1
        else:
            state.candidate_volatility = raw
            state.candidate_volatility_count = 1
        if state.candidate_volatility_count >= self.config.volatility_confirmation_bars:
            state.stable_volatility = raw
            state.candidate_volatility = VolatilityLevel.UNKNOWN
            state.candidate_volatility_count = 0

    def _decision(
        self,
        candle: Candle,
        metrics: IndicatorMetrics,
    ) -> ModuleDecision:
        created_at = candle.close_time
        valid_until = created_at + self._target_delta
        identity = f"{candle.symbol}-{self.config.timeframe}-{int(created_at.timestamp())}"
        common_metadata = {
            "module_version": MODULE_VERSION,
            "algorithm": "ema_efficiency_atr_v1",
            "config_signature": self.config.signature,
            "processed_target_bars": self._state.processed_bars,
        }
        current_value: CurrentRegimeValue = {
            "regime": self._state.stable_regime.value,
            "raw_regime": metrics.raw_regime.value,
            "trend": metrics.trend.value,
            "volatility": self._state.stable_volatility.value,
            "raw_volatility": metrics.raw_volatility.value,
            "confidence": _float(metrics.confidence),
            "as_of": created_at.isoformat(),
            "metrics": self._metrics_value(metrics),
        }
        trend_value: TrendFeatureValue = {
            "direction": metrics.trend.value,
            "regime": self._state.stable_regime.value,
            "raw_regime": metrics.raw_regime.value,
            "confidence": _float(metrics.confidence),
            "ema_fast": _float(metrics.ema_fast),
            "ema_slow": _float(metrics.ema_slow),
            "ema_spread_pct": _float(metrics.ema_spread_pct),
            "ema_slope_pct": _float(metrics.ema_slope_pct),
            "efficiency_ratio": _float(metrics.efficiency_ratio),
        }
        volatility_value: VolatilityFeatureValue = {
            "level": self._state.stable_volatility.value,
            "raw_level": metrics.raw_volatility.value,
            "atr": _float(metrics.atr),
            "atr_pct": _float(metrics.atr_pct),
            "baseline_atr_pct": _float(metrics.baseline_atr_pct),
            "ratio": _float(metrics.volatility_ratio),
        }
        features = (
            Feature(
                key=CURRENT_FEATURE_KEY,
                source_module=self.name,
                created_at=created_at,
                value=current_value,
                symbol=candle.symbol,
                timeframe=self.config.timeframe,
                valid_until=valid_until,
                schema_version=FEATURE_SCHEMA_VERSION,
                feature_id=f"mr-current-{identity}",
                invalidate_on_gap=True,
                metadata=common_metadata,
            ),
            Feature(
                key=TREND_FEATURE_KEY,
                source_module=self.name,
                created_at=created_at,
                value=trend_value,
                symbol=candle.symbol,
                timeframe=self.config.timeframe,
                valid_until=valid_until,
                schema_version=FEATURE_SCHEMA_VERSION,
                feature_id=f"mr-trend-{identity}",
                invalidate_on_gap=True,
                metadata=common_metadata,
            ),
            Feature(
                key=VOLATILITY_FEATURE_KEY,
                source_module=self.name,
                created_at=created_at,
                value=volatility_value,
                symbol=candle.symbol,
                timeframe=self.config.timeframe,
                valid_until=valid_until,
                schema_version=FEATURE_SCHEMA_VERSION,
                feature_id=f"mr-volatility-{identity}",
                invalidate_on_gap=True,
                metadata=common_metadata,
            ),
        )
        diagnostics = (
            {
                "event": "regime_published",
                "symbol": candle.symbol,
                "timeframe": self.config.timeframe,
                "regime": self._state.stable_regime.value,
                "raw_regime": metrics.raw_regime.value,
                "volatility": self._state.stable_volatility.value,
                "raw_volatility": metrics.raw_volatility.value,
                "confidence": _float(metrics.confidence),
                "health": self._health(),
                **self._metrics_value(metrics),
            }
            if self.config.publish_diagnostics
            else {}
        )
        return ModuleDecision(features=features, diagnostics=diagnostics)

    @staticmethod
    def _metrics_value(
        metrics: IndicatorMetrics,
    ) -> IndicatorMetricsValue:
        return {
            "ema_fast": _float(metrics.ema_fast),
            "ema_slow": _float(metrics.ema_slow),
            "ema_spread_pct": _float(metrics.ema_spread_pct),
            "ema_slope_pct": _float(metrics.ema_slope_pct),
            "efficiency_ratio": _float(metrics.efficiency_ratio),
            "atr": _float(metrics.atr),
            "atr_pct": _float(metrics.atr_pct),
            "baseline_atr_pct": _float(metrics.baseline_atr_pct),
            "volatility_ratio": _float(metrics.volatility_ratio),
        }

    def _skip(
        self,
        reason: SkipReason,
        **diagnostics: object,
    ) -> ModuleDecision:
        payload: Mapping[str, object]
        if self.config.publish_diagnostics:
            payload = {
                "event": "regime_skipped",
                "reason": reason.value,
                "health": self._health(),
                **diagnostics,
            }
        else:
            payload = {}
        return ModuleDecision(skips=(reason.value,), diagnostics=payload)
