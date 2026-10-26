from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
from typing import cast

from modular_core import Candle

from .config import DECIMAL_CONTEXT, MarketRegimeConfig
from .enums import Regime, TrendDirection, VolatilityLevel
from .state import RuntimeState

_ZERO = Decimal("0")
_ONE = Decimal("1")


def _d(value: float) -> Decimal:
    return Decimal(str(value))


def _clamp(
    value: Decimal,
    minimum: Decimal = _ZERO,
    maximum: Decimal = _ONE,
) -> Decimal:
    return min(max(value, minimum), maximum)


def _ratio_score(value: Decimal, threshold: Decimal) -> Decimal:
    if threshold == 0:
        return _ONE if value > 0 else _ZERO
    return _clamp(value / (threshold * Decimal("2")))


@dataclass(frozen=True, slots=True)
class IndicatorConstants:
    fast_alpha: Decimal
    slow_alpha: Decimal
    atr_period: Decimal
    atr_previous_weight: Decimal
    volatility_baseline_period: Decimal
    slope_lookback: Decimal


def build_constants(config: MarketRegimeConfig) -> IndicatorConstants:
    with localcontext(DECIMAL_CONTEXT):
        return IndicatorConstants(
            fast_alpha=Decimal("2") / Decimal(config.fast_ema_period + 1),
            slow_alpha=Decimal("2") / Decimal(config.slow_ema_period + 1),
            atr_period=Decimal(config.atr_period),
            atr_previous_weight=Decimal(config.atr_period - 1),
            volatility_baseline_period=Decimal(config.volatility_baseline_period),
            slope_lookback=Decimal(config.slope_lookback),
        )


@dataclass(frozen=True, slots=True)
class IndicatorMetrics:
    ema_fast: Decimal
    ema_slow: Decimal
    ema_spread_pct: Decimal
    ema_slope_pct: Decimal
    efficiency_ratio: Decimal
    atr: Decimal
    atr_pct: Decimal
    baseline_atr_pct: Decimal
    volatility_ratio: Decimal
    trend: TrendDirection
    raw_regime: Regime
    raw_volatility: VolatilityLevel
    confidence: Decimal


@dataclass(frozen=True, slots=True)
class IndicatorUpdate:
    metrics: IndicatorMetrics | None
    missing: tuple[str, ...]


def classify_regime(
    spread_pct: Decimal,
    slope_pct: Decimal,
    efficiency_ratio: Decimal,
    config: MarketRegimeConfig,
) -> tuple[TrendDirection, Regime]:
    bullish = (
        spread_pct >= config.trend_spread_threshold
        and slope_pct >= config.trend_slope_threshold
        and efficiency_ratio >= config.trend_efficiency_threshold
    )
    bearish = (
        spread_pct <= -config.trend_spread_threshold
        and slope_pct <= -config.trend_slope_threshold
        and efficiency_ratio >= config.trend_efficiency_threshold
    )
    if bullish:
        return TrendDirection.BULLISH, Regime.TREND_UP
    if bearish:
        return TrendDirection.BEARISH, Regime.TREND_DOWN
    if (
        efficiency_ratio <= config.range_efficiency_threshold
        and abs(spread_pct) < config.trend_spread_threshold
        and abs(slope_pct) < config.trend_slope_threshold
    ):
        return TrendDirection.NEUTRAL, Regime.RANGE
    return TrendDirection.NEUTRAL, Regime.TRANSITION


def classify_volatility(
    volatility_ratio: Decimal,
    config: MarketRegimeConfig,
) -> VolatilityLevel:
    if volatility_ratio <= config.low_volatility_ratio:
        return VolatilityLevel.LOW
    if volatility_ratio >= config.high_volatility_ratio:
        return VolatilityLevel.HIGH
    return VolatilityLevel.NORMAL


def _calculate_confidence_unscoped(
    raw_regime: Regime,
    spread_pct: Decimal,
    slope_pct: Decimal,
    efficiency_ratio: Decimal,
    config: MarketRegimeConfig,
) -> Decimal:
    if raw_regime in {Regime.TREND_UP, Regime.TREND_DOWN}:
        confidence = (
            Decimal("0.35") * _ratio_score(abs(spread_pct), config.trend_spread_threshold)
            + Decimal("0.30") * _ratio_score(abs(slope_pct), config.trend_slope_threshold)
            + Decimal("0.35")
            * _clamp(
                (efficiency_ratio - config.trend_efficiency_threshold)
                / max(
                    _ONE - config.trend_efficiency_threshold,
                    Decimal("0.0000001"),
                )
            )
        )
    elif raw_regime is Regime.RANGE:
        efficiency_score = _ONE - _clamp(
            efficiency_ratio
            / max(
                config.range_efficiency_threshold,
                Decimal("0.0000001"),
            )
        )
        spread_score = _ONE - _ratio_score(abs(spread_pct), config.trend_spread_threshold)
        slope_score = _ONE - _ratio_score(abs(slope_pct), config.trend_slope_threshold)
        confidence = (
            Decimal("0.40") * efficiency_score
            + Decimal("0.30") * spread_score
            + Decimal("0.30") * slope_score
        )
    else:
        confidence = Decimal("0.50")
    return _clamp(confidence)


def calculate_confidence(
    raw_regime: Regime,
    spread_pct: Decimal,
    slope_pct: Decimal,
    efficiency_ratio: Decimal,
    config: MarketRegimeConfig,
) -> Decimal:
    with localcontext(DECIMAL_CONTEXT):
        return _calculate_confidence_unscoped(
            raw_regime,
            spread_pct,
            slope_pct,
            efficiency_ratio,
            config,
        )


def update_indicators(
    state: RuntimeState,
    candle: Candle,
    config: MarketRegimeConfig,
    constants: IndicatorConstants,
) -> IndicatorUpdate:
    with localcontext(DECIMAL_CONTEXT):
        close = _d(candle.close)
        high = _d(candle.high)
        low = _d(candle.low)
        previous_close = state.prev_close
        state.processed_bars += 1

        if state.ema_fast is None:
            state.ema_fast = close
            state.ema_slow = close
        else:
            ema_slow = state.ema_slow
            if ema_slow is None:
                raise RuntimeError("ema_slow invariant violated after initialization")
            state.ema_fast = state.ema_fast + constants.fast_alpha * (close - state.ema_fast)
            state.ema_slow = ema_slow + constants.slow_alpha * (close - ema_slow)

        ema_slow = state.ema_slow
        if ema_slow is None:
            raise RuntimeError("ema_slow invariant violated after update")
        if len(state.slow_emas) == state.slow_emas.maxlen:
            state.slow_emas.popleft()
        state.slow_emas.append(ema_slow)

        true_range = high - low
        if previous_close is not None:
            true_range = max(
                true_range,
                abs(high - previous_close),
                abs(low - previous_close),
            )
        if state.atr is None:
            state.tr_seed.append(true_range)
            if len(state.tr_seed) == config.atr_period:
                state.atr = sum(state.tr_seed, _ZERO) / constants.atr_period
                state.tr_seed.clear()
        else:
            state.atr = (
                state.atr * constants.atr_previous_weight + true_range
            ) / constants.atr_period

        if previous_close is not None:
            movement = abs(close - previous_close)
            if len(state.movements) == state.movements.maxlen:
                state.movement_sum -= state.movements.popleft()
            state.movements.append(movement)
            state.movement_sum += movement
        if len(state.closes) == state.closes.maxlen:
            state.closes.popleft()
        state.closes.append(close)

        baseline_atr_pct: Decimal | None = None
        atr_pct: Decimal | None = None
        volatility_ratio: Decimal | None = None
        if state.atr is not None:
            atr_pct = state.atr / close
            if len(state.atr_pct_window) == config.volatility_baseline_period:
                baseline_atr_pct = state.atr_pct_sum / constants.volatility_baseline_period
                if baseline_atr_pct > 0:
                    volatility_ratio = atr_pct / baseline_atr_pct
                elif atr_pct == 0:
                    volatility_ratio = _ONE
                else:
                    volatility_ratio = max(
                        config.high_volatility_ratio * Decimal("2"),
                        Decimal("2"),
                    )

        efficiency_ratio: Decimal | None = None
        if (
            len(state.closes) == config.efficiency_period + 1
            and len(state.movements) == config.efficiency_period
        ):
            net_move = abs(state.closes[-1] - state.closes[0])
            efficiency_ratio = _ZERO if state.movement_sum == 0 else net_move / state.movement_sum

        ema_slope_pct: Decimal | None = None
        if len(state.slow_emas) == config.slope_lookback + 1:
            past_ema = state.slow_emas[0]
            if past_ema > 0:
                ema_slope_pct = (
                    (state.slow_emas[-1] - past_ema) / past_ema / constants.slope_lookback
                )

        state.prev_close = close

        missing: list[str] = []
        if state.processed_bars < config.slow_ema_period:
            missing.append("ema_warmup")
        if efficiency_ratio is None:
            missing.append("efficiency_warmup")
        if ema_slope_pct is None:
            missing.append("slope_warmup")
        if state.atr is None:
            missing.append("atr_warmup")
        if baseline_atr_pct is None or volatility_ratio is None:
            missing.append("volatility_baseline_warmup")

        if atr_pct is not None:
            if len(state.atr_pct_window) == state.atr_pct_window.maxlen:
                state.atr_pct_sum -= state.atr_pct_window.popleft()
            state.atr_pct_window.append(atr_pct)
            state.atr_pct_sum += atr_pct

        if missing:
            return IndicatorUpdate(metrics=None, missing=tuple(missing))

        ema_fast = state.ema_fast
        ema_slow = state.ema_slow
        atr = cast(Decimal, state.atr)
        atr_pct_value = cast(Decimal, atr_pct)
        baseline_atr_pct_value = cast(Decimal, baseline_atr_pct)
        volatility_ratio_value = cast(Decimal, volatility_ratio)
        efficiency_ratio_value = cast(Decimal, efficiency_ratio)
        ema_slope_pct_value = cast(Decimal, ema_slope_pct)

        spread_pct = (ema_fast - ema_slow) / close
        trend, raw_regime = classify_regime(
            spread_pct, ema_slope_pct_value, efficiency_ratio_value, config
        )
        raw_volatility = classify_volatility(volatility_ratio_value, config)
        confidence = _calculate_confidence_unscoped(
            raw_regime,
            spread_pct,
            ema_slope_pct_value,
            efficiency_ratio_value,
            config,
        )

        return IndicatorUpdate(
            metrics=IndicatorMetrics(
                ema_fast=ema_fast,
                ema_slow=ema_slow,
                ema_spread_pct=spread_pct,
                ema_slope_pct=ema_slope_pct_value,
                efficiency_ratio=efficiency_ratio_value,
                atr=atr,
                atr_pct=atr_pct_value,
                baseline_atr_pct=baseline_atr_pct_value,
                volatility_ratio=volatility_ratio_value,
                trend=trend,
                raw_regime=raw_regime,
                raw_volatility=raw_volatility,
                confidence=confidence,
            ),
            missing=(),
        )
