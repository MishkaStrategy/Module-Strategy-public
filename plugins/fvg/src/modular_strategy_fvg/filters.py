from __future__ import annotations

from decimal import Decimal, DecimalException
from types import MappingProxyType

from .decimal_utils import deterministic_decimal
from .detector import _optional_candle_attribute
from .config import FvgConfig, FvgGapSizeMode, FvgPriceFilterMode, FvgPriceMatchPolicy
from .detector import as_decimal
from .models import FvgFilterDecision, FvgFilterTrace, FvgMetrics, FvgZone


def _bounded_trace(
    *,
    name: str,
    observed: Decimal | None,
    minimum: Decimal | None,
    maximum: Decimal | None,
    unavailable_reason: str,
    metadata: dict[str, object] | None = None,
) -> FvgFilterTrace:
    if observed is None:
        return FvgFilterTrace(
            name=name,
            decision=FvgFilterDecision.UNAVAILABLE,
            reason=unavailable_reason,
            minimum=minimum,
            maximum=maximum,
            metadata=MappingProxyType(metadata or {}),
        )
    if minimum is not None and observed < minimum:
        return FvgFilterTrace(
            name=name,
            decision=FvgFilterDecision.FAILED,
            reason=f"{name}_below_minimum",
            observed=observed,
            minimum=minimum,
            maximum=maximum,
            metadata=MappingProxyType(metadata or {}),
        )
    if maximum is not None and observed > maximum:
        return FvgFilterTrace(
            name=name,
            decision=FvgFilterDecision.FAILED,
            reason=f"{name}_above_maximum",
            observed=observed,
            minimum=minimum,
            maximum=maximum,
            metadata=MappingProxyType(metadata or {}),
        )
    return FvgFilterTrace(
        name=name,
        decision=FvgFilterDecision.PASSED,
        reason=f"{name}_within_bounds",
        observed=observed,
        minimum=minimum,
        maximum=maximum,
        metadata=MappingProxyType(metadata or {}),
    )


@deterministic_decimal
def evaluate_gap_size_filter(metrics: FvgMetrics, config: FvgConfig) -> FvgFilterTrace:
    if not isinstance(metrics, FvgMetrics):
        raise TypeError("metrics must be FvgMetrics")
    if not isinstance(config, FvgConfig):
        raise TypeError("config must be an FvgConfig")
    if config.gap_size_mode is FvgGapSizeMode.LEGACY:
        absolute_ok = metrics.gap_size >= config.min_gap_absolute
        fraction_ok = metrics.gap_fraction >= config.min_gap_fraction
        if absolute_ok and fraction_ok:
            decision = FvgFilterDecision.PASSED
            reason = "gap_size_legacy_within_bounds"
        else:
            decision = FvgFilterDecision.FAILED
            reason = "gap_size_legacy_below_minimum"
        return FvgFilterTrace(
            name="gap_size",
            decision=decision,
            reason=reason,
            observed=metrics.gap_size,
            minimum=config.min_gap_absolute,
            metadata=MappingProxyType(
                {
                    "mode": config.gap_size_mode.value,
                    "gap_fraction": str(metrics.gap_fraction),
                    "minimum_fraction": str(config.min_gap_fraction),
                }
            ),
        )

    observed = {
        FvgGapSizeMode.ABSOLUTE: metrics.gap_size,
        FvgGapSizeMode.FRACTION: metrics.gap_fraction,
        FvgGapSizeMode.BPS: metrics.gap_bps,
        FvgGapSizeMode.TICKS: metrics.gap_ticks,
        FvgGapSizeMode.ATR: metrics.gap_atr,
        FvgGapSizeMode.B_RANGE: metrics.gap_b_range,
        FvgGapSizeMode.AVERAGE_RANGE: metrics.gap_average_range,
    }[config.gap_size_mode]
    return _bounded_trace(
        name="gap_size",
        observed=observed,
        minimum=config.min_gap_size,
        maximum=config.max_gap_size,
        unavailable_reason="gap_size_metric_unavailable",
        metadata={"mode": config.gap_size_mode.value},
    )


def _price_bounds(
    config: FvgConfig, reference_price: Decimal | None
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if config.price_filter_mode is FvgPriceFilterMode.DISABLED:
        return None, None, reference_price
    if config.price_filter_mode is FvgPriceFilterMode.ABSOLUTE:
        return config.price_filter_min, config.price_filter_max, reference_price
    if config.price_filter_mode is FvgPriceFilterMode.ANCHOR_FRACTION:
        reference_price = config.price_filter_anchor
    if reference_price is None:
        return None, None, None
    if reference_price <= 0:
        raise ValueError("price filter reference must be positive")
    lower = reference_price * (Decimal("1") - config.price_filter_lower_fraction)
    upper = reference_price * (Decimal("1") + config.price_filter_upper_fraction)
    return lower, upper, reference_price


@deterministic_decimal
def evaluate_price_filter(
    zone: FvgZone,
    config: FvgConfig,
    *,
    reference_price: Decimal | None,
) -> FvgFilterTrace:
    if not isinstance(zone, FvgZone):
        raise TypeError("zone must be an FvgZone")
    if not isinstance(config, FvgConfig):
        raise TypeError("config must be an FvgConfig")
    if reference_price is not None:
        if type(reference_price) is not Decimal:
            raise TypeError("reference_price must be a Decimal or null")
        if not reference_price.is_finite():
            raise ValueError("price filter reference must be finite")
        if reference_price <= 0:
            raise ValueError("price filter reference must be positive")
    if config.price_filter_mode is FvgPriceFilterMode.DISABLED:
        return FvgFilterTrace(
            name="price",
            decision=FvgFilterDecision.SKIPPED,
            reason="price_filter_disabled",
            metadata=MappingProxyType({"mode": config.price_filter_mode.value}),
        )

    try:
        lower, upper, effective_reference = _price_bounds(config, reference_price)
    except DecimalException:
        return FvgFilterTrace(
            name="price",
            decision=FvgFilterDecision.UNAVAILABLE,
            reason="price_filter_arithmetic_unavailable",
            metadata=MappingProxyType({"mode": config.price_filter_mode.value}),
        )
    if (
        config.price_filter_mode
        in {
            FvgPriceFilterMode.CURRENT_FRACTION,
            FvgPriceFilterMode.ANCHOR_FRACTION,
        }
        and effective_reference is None
    ):
        return FvgFilterTrace(
            name="price",
            decision=FvgFilterDecision.UNAVAILABLE,
            reason="price_reference_unavailable",
            metadata=MappingProxyType({"mode": config.price_filter_mode.value}),
        )

    if config.price_filter_match_policy is FvgPriceMatchPolicy.MIDPOINT:
        observed = zone.midpoint
        passed = (lower is None or observed >= lower) and (upper is None or observed <= upper)
    elif config.price_filter_match_policy is FvgPriceMatchPolicy.OVERLAP:
        observed = zone.midpoint
        passed = (lower is None or zone.upper >= lower) and (upper is None or zone.lower <= upper)
    else:
        observed = zone.midpoint
        passed = (lower is None or zone.lower >= lower) and (upper is None or zone.upper <= upper)

    return FvgFilterTrace(
        name="price",
        decision=FvgFilterDecision.PASSED if passed else FvgFilterDecision.FAILED,
        reason="price_filter_match" if passed else "price_filter_no_match",
        observed=observed,
        minimum=lower,
        maximum=upper,
        metadata=MappingProxyType(
            {
                "mode": config.price_filter_mode.value,
                "match_policy": config.price_filter_match_policy.value,
                "reference_price": None if effective_reference is None else str(effective_reference),
                "zone_lower": str(zone.lower),
                "zone_upper": str(zone.upper),
            }
        ),
    )


def _volume(candle: object) -> Decimal | None:
    raw = _optional_candle_attribute(candle, "volume filter", "volume")
    if raw is None:
        return None
    value = as_decimal(raw)
    if value < 0:
        raise ValueError("volume must be finite and non-negative")
    return value


def _volume_missing(config: FvgConfig, reason: str) -> FvgFilterTrace:
    from .config import FvgMissingDataPolicy

    if config.volume_missing_policy is FvgMissingDataPolicy.ERROR:
        raise ValueError(reason)
    if config.volume_missing_policy is FvgMissingDataPolicy.ALLOW:
        return FvgFilterTrace(
            name="volume",
            decision=FvgFilterDecision.SKIPPED,
            reason=f"{reason}_allowed",
            metadata=MappingProxyType({"mode": config.volume_filter_mode.value}),
        )
    return FvgFilterTrace(
        name="volume",
        decision=FvgFilterDecision.FAILED,
        reason=reason,
        metadata=MappingProxyType({"mode": config.volume_filter_mode.value}),
    )


def _ema(values: list[Decimal]) -> Decimal:
    alpha = Decimal("2") / Decimal(len(values) + 1)
    result = values[0]
    for value in values[1:]:
        result = alpha * value + (Decimal("1") - alpha) * result
    return result


def _median(values: list[Decimal]) -> Decimal:
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / Decimal("2")


@deterministic_decimal
def evaluate_volume_filter(
    *,
    a: object,
    b: object,
    c: object,
    previous_bars: tuple[object, ...],
    config: FvgConfig,
) -> FvgFilterTrace:
    from .config import FvgVolumeFilterMode

    if not isinstance(config, FvgConfig):
        raise TypeError("config must be an FvgConfig")
    if type(previous_bars) is not tuple:
        raise TypeError("previous_bars must be a canonical tuple")
    if config.volume_filter_mode is FvgVolumeFilterMode.DISABLED:
        return FvgFilterTrace(
            name="volume",
            decision=FvgFilterDecision.SKIPPED,
            reason="volume_filter_disabled",
            metadata=MappingProxyType({"mode": config.volume_filter_mode.value}),
        )
    if len(previous_bars) < config.volume_lookback:
        return _volume_missing(config, "volume_history_insufficient")

    prior_values = [_volume(bar) for bar in previous_bars[-config.volume_lookback :]]
    b_volume = _volume(b)
    if any(value is None for value in (*prior_values, b_volume)):
        return _volume_missing(config, "volume_data_missing")
    prior = [value for value in prior_values if value is not None]
    if b_volume is None:
        return _volume_missing(config, "volume_data_missing")

    mode = config.volume_filter_mode
    a_volume: Decimal | None = None
    c_volume: Decimal | None = None
    if mode is FvgVolumeFilterMode.ABC_TO_SMA:
        a_volume, c_volume = _volume(a), _volume(c)
        if a_volume is None or c_volume is None:
            return _volume_missing(config, "volume_data_missing")

    numerator: Decimal
    reference: Decimal
    try:
        if mode is FvgVolumeFilterMode.B_TO_SMA:
            numerator = b_volume
            reference = sum(prior, Decimal("0")) / Decimal(len(prior))
        elif mode is FvgVolumeFilterMode.B_TO_EMA:
            numerator = b_volume
            reference = _ema(prior)
        elif mode is FvgVolumeFilterMode.B_TO_MEDIAN:
            numerator = b_volume
            reference = _median(prior)
        elif mode is FvgVolumeFilterMode.ABC_TO_SMA:
            if a_volume is None or c_volume is None:
                return _volume_missing(config, "volume_data_missing")
            numerator = a_volume + b_volume + c_volume
            reference = (sum(prior, Decimal("0")) / Decimal(len(prior))) * Decimal("3")
        else:
            numerator = b_volume
            reference = Decimal(len(prior))
            ratio = Decimal(sum(1 for value in prior if value <= b_volume)) / reference
            return _bounded_trace(
                name="volume",
                observed=ratio,
                minimum=config.volume_min_ratio,
                maximum=config.volume_max_ratio,
                unavailable_reason="volume_metric_unavailable",
                metadata={
                    "mode": mode.value,
                    "lookback": config.volume_lookback,
                    "numerator": str(numerator),
                    "reference": "percentile_rank",
                },
            )

        if reference == 0:
            return _volume_missing(config, "volume_reference_zero")
        ratio = numerator / reference
    except DecimalException as exc:
        raise ValueError("volume arithmetic exceeds supported decimal range") from exc
    return _bounded_trace(
        name="volume",
        observed=ratio,
        minimum=config.volume_min_ratio,
        maximum=config.volume_max_ratio,
        unavailable_reason="volume_metric_unavailable",
        metadata={
            "mode": mode.value,
            "lookback": config.volume_lookback,
            "numerator": str(numerator),
            "reference": str(reference),
        },
    )
