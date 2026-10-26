from __future__ import annotations

from decimal import Decimal

from .config import LiquiditySweepConfig
from .models import BarSnapshot, LiquidityLevel, LiquiditySide, SweepDirection, SweepMeasurement
from .reasons import LiquiditySweepReason

_BPS = Decimal("10000")
BarGeometry = tuple[Decimal, Decimal, Decimal, Decimal, bool, bool]


class SweepEvaluator:
    """Precomputed, side-symmetric sweep math for the module hot path."""

    def __init__(self, config: LiquiditySweepConfig) -> None:
        self.config = config
        self.min_penetration = Decimal(str(config.min_penetration_bps))
        self.max_penetration = Decimal(str(config.max_penetration_bps))
        reclaim = Decimal(str(config.close_reclaim_bps)) / _BPS
        invalidation = Decimal(str(config.invalidation_close_bps)) / _BPS
        self.high_reclaim_factor = Decimal("1") - reclaim
        self.low_reclaim_factor = Decimal("1") + reclaim
        self.high_invalidation_factor = Decimal("1") + invalidation
        self.low_invalidation_factor = Decimal("1") - invalidation
        self.min_wick_ratio = Decimal(str(config.min_wick_to_body_ratio))
        self.min_wick_fraction = Decimal(str(config.min_wick_fraction))
        self.signal_ratio_target = max(self.min_wick_ratio * 2, Decimal("1"))

    @staticmethod
    def geometry(bar: BarSnapshot) -> BarGeometry:
        body = abs(bar.close - bar.open)
        bar_range = bar.high - bar.low
        upper_wick = bar.high - max(bar.open, bar.close)
        lower_wick = min(bar.open, bar.close) - bar.low
        return body, bar_range, upper_wick, lower_wick, bar.close < bar.open, bar.close > bar.open

    def evaluate(
        self,
        level: LiquidityLevel,
        bar: BarSnapshot,
        geometry: BarGeometry,
    ) -> tuple[SweepMeasurement | None, LiquiditySweepReason | None]:
        if (bar.symbol, bar.timeframe) != (level.symbol, level.timeframe):
            return None, None
        body, bar_range, upper_wick, lower_wick, bearish_close, bullish_close = geometry
        if bar_range <= 0:
            return None, LiquiditySweepReason.ZERO_RANGE

        if level.side is LiquiditySide.HIGH:
            if bar.high <= level.price:
                return None, None
            penetration = ((bar.high - level.price) / level.price) * _BPS
            reclaimed = bar.close <= level.price * self.high_reclaim_factor
            directional = bearish_close
            direction = SweepDirection.BEARISH
            extreme = bar.high
            wick = upper_wick
        else:
            if bar.low >= level.price:
                return None, None
            penetration = ((level.price - bar.low) / level.price) * _BPS
            reclaimed = bar.close >= level.price * self.low_reclaim_factor
            directional = bullish_close
            direction = SweepDirection.BULLISH
            extreme = bar.low
            wick = lower_wick

        if penetration < self.min_penetration:
            return None, LiquiditySweepReason.PENETRATION_BELOW_MIN
        if penetration > self.max_penetration:
            return None, LiquiditySweepReason.PENETRATION_ABOVE_MAX
        if not reclaimed:
            return None, LiquiditySweepReason.CLOSE_NOT_RECLAIMED
        if self.config.require_directional_close and not directional:
            return None, LiquiditySweepReason.DIRECTIONAL_CLOSE_REQUIRED
        wick_ratio = self._wick_ratio(wick, body)
        wick_fraction = wick / bar_range
        if wick_ratio < self.min_wick_ratio:
            return None, LiquiditySweepReason.WICK_RATIO_BELOW_MIN
        if wick_fraction < self.min_wick_fraction:
            return None, LiquiditySweepReason.WICK_FRACTION_BELOW_MIN
        return (
            SweepMeasurement(
                direction=direction,
                penetration_bps=penetration,
                wick_to_body_ratio=wick_ratio,
                wick_fraction=wick_fraction,
                extreme_price=extreme,
            ),
            None,
        )

    @staticmethod
    def _wick_ratio(wick: Decimal, body: Decimal) -> Decimal:
        """Return scale-independent wick/body geometry for doji and tiny prices."""
        if body != 0:
            return wick / body
        if wick > 0:
            return Decimal("Infinity")
        return Decimal("0")

    def is_invalidated(self, level: LiquidityLevel, bar: BarSnapshot) -> bool:
        if level.side is LiquiditySide.HIGH:
            return bar.close > level.price * self.high_invalidation_factor
        return bar.close < level.price * self.low_invalidation_factor

    def signal_strength(self, measurement: SweepMeasurement) -> float:
        ratio_component = min(
            measurement.wick_to_body_ratio / self.signal_ratio_target, Decimal("1")
        )
        fraction_component = min(measurement.wick_fraction, Decimal("1"))
        return float((ratio_component + fraction_component) / Decimal("2"))
