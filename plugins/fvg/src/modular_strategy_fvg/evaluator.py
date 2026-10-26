from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from types import MappingProxyType
from typing import Sequence

from .analysis import _calculate_metrics_validated
from .config import FvgConfig, FvgFilterTarget
from .decimal_utils import deterministic_decimal
from .detector import (
    CandleLike,
    _detect_geometry_validated,
    _required_candle_attribute,
    _validate_candle,
    _validate_candle_clock,
    as_decimal,
    validate_history_bars,
)
from .filters import (
    evaluate_gap_size_filter,
    evaluate_price_filter,
    evaluate_volume_filter,
)
from .models import (
    FvgEvaluation,
    FvgEvaluationStatus,
    FvgFilterDecision,
    FvgFilterTrace,
)
from .timeframes import as_utc


def _confirmed_filter_applies(configured: FvgFilterTarget) -> bool:
    return configured in {
        FvgFilterTarget.BOTH,
        FvgFilterTarget.CONFIRMED,
    }


def _target_skipped(name: str, configured: FvgFilterTarget) -> FvgFilterTrace:
    return FvgFilterTrace(
        name=name,
        decision=FvgFilterDecision.SKIPPED,
        reason=f"{name}_filter_target_not_applicable",
        metadata=MappingProxyType(
            {
                "configured_target": configured.value,
                "event_target": FvgFilterTarget.CONFIRMED.value,
            }
        ),
    )


def _status_for(
    filters: Sequence[FvgFilterTrace],
) -> tuple[FvgEvaluationStatus, str]:
    for trace in filters:
        if trace.decision is FvgFilterDecision.FAILED:
            return FvgEvaluationStatus.REJECTED, trace.reason
    for trace in filters:
        if trace.decision is FvgFilterDecision.UNAVAILABLE:
            return (
                FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA,
                trace.reason,
            )
    return FvgEvaluationStatus.ACCEPTED, "fvg_accepted"


@deterministic_decimal
def evaluate_fvg_window(
    *,
    a: CandleLike,
    b: CandleLike,
    c: CandleLike,
    previous_bars: Sequence[CandleLike] = (),
    config: FvgConfig,
    reference_price: Decimal | None = None,
    available_at: datetime | None = None,
) -> FvgEvaluation:
    """Evaluate one confirmed, point-in-time three-candle FVG window."""

    if not isinstance(config, FvgConfig):
        raise TypeError("config must be an FvgConfig")
    _, c_close_time = _validate_candle_clock(c, "c")
    evaluated_at = as_utc(c_close_time if available_at is None else available_at)
    if evaluated_at < c_close_time:
        return FvgEvaluation(
            status=FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            zone=None,
            metrics=None,
            reason="confirmed_fvg_unavailable_before_c_close",
            evaluated_at=evaluated_at,
        )

    for label, candle in (("a", a), ("b", b), ("c", c)):
        _validate_candle(candle, label)

    a_symbol = _required_candle_attribute(a, "a", "symbol")
    a_timeframe = _required_candle_attribute(a, "a", "timeframe")
    a_timestamp = _required_candle_attribute(a, "a", "timestamp")
    if type(a_symbol) is not str or type(a_timeframe) is not str:
        raise TypeError("source A symbol and timeframe must be canonical strings")
    if not isinstance(a_timestamp, datetime):
        raise TypeError("source A timestamp must be a datetime")
    validate_history_bars(
        previous_bars,
        symbol=a_symbol,
        timeframe=a_timeframe,
        before=a_timestamp,
        require_consecutive=config.require_consecutive_bars,
    )

    zone = _detect_geometry_validated(a, b, c, config)
    if zone is None:
        return FvgEvaluation(
            status=FvgEvaluationStatus.REJECTED,
            zone=None,
            metrics=None,
            reason="fvg_geometry_not_present",
            evaluated_at=evaluated_at,
        )

    volume_trace = (
        evaluate_volume_filter(
            a=a,
            b=b,
            c=c,
            previous_bars=tuple(previous_bars),
            config=config,
        )
        if _confirmed_filter_applies(config.volume_filter_target)
        else _target_skipped("volume", config.volume_filter_target)
    )
    history = tuple(previous_bars) + (a, b, c)
    raw_reference = _required_candle_attribute(c, "c", "close") if reference_price is None else reference_price
    effective_reference = as_decimal(raw_reference)
    if effective_reference <= 0:
        raise ValueError("reference_price must be positive")
    try:
        metrics = _calculate_metrics_validated(
            zone=zone,
            a=a,
            b=b,
            c=c,
            history=history,
            reference_price=effective_reference,
            tick_size=config.tick_size,
            atr_period=config.atr_period,
            average_range_period=config.average_range_period,
            volume_ratio=volume_trace.observed,
        )
    except ValueError as exc:
        if str(exc) != "metric arithmetic exceeds supported decimal range":
            raise
        return FvgEvaluation(
            status=FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            zone=None,
            metrics=None,
            reason=str(exc),
            evaluated_at=evaluated_at,
        )
    gap_trace = evaluate_gap_size_filter(metrics, config)
    price_trace = (
        evaluate_price_filter(zone, config, reference_price=effective_reference)
        if _confirmed_filter_applies(config.price_filter_target)
        else _target_skipped("price", config.price_filter_target)
    )
    traces = (gap_trace, price_trace, volume_trace)
    status, reason = _status_for(traces)
    return FvgEvaluation(
        status=status,
        zone=zone if status is FvgEvaluationStatus.ACCEPTED else None,
        metrics=metrics,
        filters=traces,
        reason=reason,
        evaluated_at=evaluated_at,
    )
