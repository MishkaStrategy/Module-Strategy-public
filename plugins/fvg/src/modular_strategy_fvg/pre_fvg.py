from __future__ import annotations

import hashlib
from collections.abc import Sequence as SequenceABC
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, DecimalException
from typing import Sequence

from .analysis import _calculate_metrics_validated
from .config import FvgConfig, FvgDirectionFilter, FvgFilterTarget
from .decimal_utils import canonical_decimal_text, deterministic_decimal
from .detector import (
    CandleLike,
    _optional_candle_attribute,
    _required_candle_attribute,
    _validate_candle,
    as_decimal,
    validate_history_bars,
)
from .filters import evaluate_gap_size_filter, evaluate_price_filter, evaluate_volume_filter
from .models import (
    FvgDirection,
    FvgFilterDecision,
    FvgFilterTrace,
    FvgPreCandidate,
    FvgPreEvaluation,
    FvgPreStatus,
    FvgZone,
    _require_canonical_symbol,
)
from .timeframes import as_utc, canonical_timeframe, is_period_aligned, period_close, timeframe_seconds


def _lower_bar_clock(bar: object, label: str) -> tuple[datetime, datetime]:
    try:
        opened = getattr(bar, "timestamp")
        closed = getattr(bar, "close_time")
    except AttributeError as exc:
        raise TypeError(f"{label} must expose timestamp and close_time") from exc
    except Exception as exc:
        raise ValueError(f"{label} candle clock cannot be read") from exc
    return as_utc(opened, f"{label}.timestamp"), as_utc(closed, f"{label}.close_time")


@dataclass(frozen=True, slots=True)
class ProvisionalCandle:
    timestamp: datetime
    close_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    symbol: str
    timeframe: str

    @deterministic_decimal
    def __post_init__(self) -> None:
        if type(self.symbol) is not str or type(self.timeframe) is not str:
            raise TypeError("symbol and timeframe must be canonical strings")
        _require_canonical_symbol(self.symbol)
        if not self.timeframe:
            raise ValueError("timeframe is required")
        if canonical_timeframe(self.timeframe) != self.timeframe:
            raise ValueError("timeframe must be canonical")
        opened = as_utc(self.timestamp, "timestamp")
        closed = as_utc(self.close_time, "close_time")
        if not is_period_aligned(opened, self.timeframe):
            raise ValueError("provisional candle open time is not aligned to timeframe")
        if closed - opened != timedelta(seconds=timeframe_seconds(self.timeframe)):
            raise ValueError("provisional candle duration does not match timeframe")
        for name in ("open", "high", "low", "close"):
            value = getattr(self, name)
            if type(value) is not Decimal:
                raise TypeError(f"{name} must be a Decimal")
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.low > self.high or not self.low <= self.open <= self.high or not self.low <= self.close <= self.high:
            raise ValueError("provisional candle price geometry is invalid")
        if self.volume is not None:
            if type(self.volume) is not Decimal:
                raise TypeError("volume must be a Decimal or null")
            if not self.volume.is_finite() or self.volume < 0:
                raise ValueError("volume must be finite and non-negative")
        object.__setattr__(self, "timestamp", opened)
        object.__setattr__(self, "close_time", closed)


@deterministic_decimal
def aggregate_provisional_c(
    *,
    lower_bars: Sequence[CandleLike],
    symbol: str,
    target_timeframe: str,
    target_open_time: datetime,
    cutoff_time: datetime,
) -> ProvisionalCandle:
    if isinstance(lower_bars, (str, bytes)) or not isinstance(lower_bars, SequenceABC):
        raise TypeError("lower_bars must be an ordered sequence of candle values")
    if type(symbol) is not str:
        raise TypeError("symbol must be a canonical string")
    _require_canonical_symbol(symbol)
    if type(target_timeframe) is not str:
        raise TypeError("target_timeframe must be a canonical string")
    canonical_target = canonical_timeframe(target_timeframe)
    if canonical_target != target_timeframe:
        raise ValueError("target_timeframe must be canonical")
    target_open = as_utc(target_open_time, "target_open_time")
    cutoff = as_utc(cutoff_time, "cutoff_time")
    target_seconds = timeframe_seconds(canonical_target)
    if not is_period_aligned(target_open, canonical_target):
        raise ValueError("target_open_time is not aligned to target timeframe")
    target_close = period_close(target_open, canonical_target)
    if not target_open < cutoff < target_close:
        raise ValueError("cutoff_time must be inside the forming target candle")

    selected_rows: list[CandleLike] = []
    for index, bar in enumerate(lower_bars):
        opened, closed = _lower_bar_clock(bar, f"lower_bars[{index}]")
        if opened >= target_open and closed <= cutoff:
            selected_rows.append(bar)
    selected = tuple(selected_rows)
    if not selected:
        raise ValueError("lower_timeframe_history_unavailable")
    lower_timeframe = _required_candle_attribute(selected[0], "selected_lower_bars[0]", "timeframe")
    if type(lower_timeframe) is not str:
        raise TypeError("selected lower bar timeframe must be a canonical string")
    lower_seconds = timeframe_seconds(lower_timeframe)
    if lower_seconds >= target_seconds or target_seconds % lower_seconds:
        raise ValueError("lower_timeframe_resolution_incompatible")
    expected_open = target_open
    for bar in selected:
        _validate_candle(bar, "lower_bar")
        if bar.symbol != symbol or bar.timeframe != lower_timeframe:
            raise ValueError("lower_timeframe_series_contract_mismatch")
        opened = as_utc(bar.timestamp)
        closed = as_utc(bar.close_time)
        if opened != expected_open or int((closed - opened).total_seconds()) != lower_seconds:
            raise ValueError("lower_timeframe_history_not_contiguous")
        if closed > cutoff:
            raise ValueError("future_lower_timeframe_bar_exposed")
        expected_open = closed
    if expected_open != cutoff:
        raise ValueError("lower_timeframe_resolution_cannot_reach_evaluation_offset")

    raw_open = _optional_candle_attribute(selected[0], "selected_lower_bars[0]", "open")
    if raw_open is None:
        raise ValueError("lower_timeframe_open_unavailable")
    raw_volumes = [
        _optional_candle_attribute(bar, f"selected_lower_bars[{index}]", "volume") for index, bar in enumerate(selected)
    ]
    try:
        volume = (
            None
            if any(value is None for value in raw_volumes)
            else sum((as_decimal(value) for value in raw_volumes), Decimal("0"))
        )
    except DecimalException as exc:
        raise ValueError("volume arithmetic exceeds supported decimal range") from exc
    return ProvisionalCandle(
        timestamp=target_open,
        close_time=target_close,
        open=as_decimal(raw_open),
        high=max(
            as_decimal(_required_candle_attribute(bar, f"selected_lower_bars[{index}]", "high"))
            for index, bar in enumerate(selected)
        ),
        low=min(
            as_decimal(_required_candle_attribute(bar, f"selected_lower_bars[{index}]", "low"))
            for index, bar in enumerate(selected)
        ),
        close=as_decimal(
            _required_candle_attribute(
                selected[-1],
                f"selected_lower_bars[{len(selected) - 1}]",
                "close",
            )
        ),
        volume=volume,
        symbol=symbol,
        timeframe=canonical_target,
    )


def make_pre_candidate_id(
    *,
    symbol: str,
    timeframe: str,
    direction: FvgDirection | str,
    source_c_open_time: datetime,
    evaluated_at: datetime,
    lower: Decimal,
    upper: Decimal,
) -> str:
    if type(symbol) is not str:
        raise TypeError("symbol must be a canonical string")
    _require_canonical_symbol(symbol)
    if type(timeframe) is not str:
        raise TypeError("timeframe must be a canonical string")
    canonical_tf = canonical_timeframe(timeframe)
    if canonical_tf != timeframe:
        raise ValueError("timeframe must be canonical")
    if isinstance(direction, FvgDirection):
        canonical_direction = direction
    elif type(direction) is str:
        canonical_direction = FvgDirection(direction)
    else:
        raise TypeError("direction must be an FvgDirection or canonical string")
    if type(lower) is not Decimal or type(upper) is not Decimal:
        raise TypeError("lower and upper must be Decimal values")
    if not lower.is_finite() or not upper.is_finite() or lower <= 0 or upper <= lower:
        raise ValueError("pre-candidate bounds must be finite, positive, and ordered")
    source_open = as_utc(source_c_open_time, "source_c_open_time")
    evaluated = as_utc(evaluated_at, "evaluated_at")
    if not is_period_aligned(source_open, canonical_tf):
        raise ValueError("source_c_open_time is not aligned to timeframe")
    source_close = period_close(source_open, canonical_tf)
    if not source_open < evaluated < source_close:
        raise ValueError("evaluated_at must be inside the forming source C candle")
    identity = "|".join(
        (
            symbol,
            canonical_tf,
            canonical_direction.value,
            source_open.isoformat(),
            evaluated.isoformat(),
            canonical_decimal_text(lower),
            canonical_decimal_text(upper),
        )
    )
    return f"pre-fvg:{hashlib.sha256(identity.encode('utf-8')).hexdigest()[:24]}"


def _pre_filter_applies(target: FvgFilterTarget) -> bool:
    return target in {FvgFilterTarget.BOTH, FvgFilterTarget.PRE_FVG}


def _skipped(name: str, target: FvgFilterTarget) -> FvgFilterTrace:
    return FvgFilterTrace(
        name=name,
        decision=FvgFilterDecision.SKIPPED,
        reason=f"{name}_filter_target_not_applicable",
        metadata={"configured_target": target.value, "event_target": "pre_fvg"},
    )


@deterministic_decimal
def evaluate_pre_fvg(
    *,
    a: CandleLike,
    b: CandleLike,
    lower_bars: Sequence[CandleLike],
    observed_at: datetime,
    config: FvgConfig,
    previous_target_bars: Sequence[CandleLike] = (),
    reference_price: Decimal | None = None,
) -> FvgPreEvaluation:
    """Evaluate provisional C strictly at the configured point-in-time cutoff."""

    if not isinstance(config, FvgConfig):
        raise TypeError("config must be an FvgConfig")
    observed = as_utc(observed_at, "observed_at")
    if not config.pre_fvg_enabled:
        return FvgPreEvaluation(
            status=FvgPreStatus.REJECTED,
            reason="pre_fvg_disabled",
            processed_at=observed,
        )
    if isinstance(lower_bars, (str, bytes)) or not isinstance(lower_bars, SequenceABC):
        raise TypeError("lower_bars must be an ordered sequence of candle values")
    _validate_candle(a, "a")
    _validate_candle(b, "b")
    if a.symbol != b.symbol or a.timeframe != b.timeframe:
        raise ValueError("pre-FVG A/B candles must share symbol and timeframe")
    if observed < as_utc(b.close_time, "b.close_time"):
        raise ValueError("pre-FVG source A/B candles must be closed by observed_at")
    if b.timeframe not in config.timeframes:
        raise ValueError(f"pre-FVG source timeframe is not configured: {b.timeframe}")
    validate_history_bars(
        previous_target_bars,
        symbol=a.symbol,
        timeframe=a.timeframe,
        before=a.timestamp,
        require_consecutive=config.require_consecutive_bars,
    )
    if config.require_consecutive_bars and as_utc(b.timestamp) != as_utc(a.close_time):
        return FvgPreEvaluation(
            status=FvgPreStatus.REJECTED,
            reason="pre_fvg_ab_not_consecutive",
            processed_at=observed,
        )

    c_open = as_utc(b.close_time)
    target_close = period_close(c_open, b.timeframe)
    target_evaluation = target_close - timedelta(seconds=timeframe_seconds(config.pre_fvg_evaluation_offset))
    if observed >= target_close:
        return FvgPreEvaluation(
            status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            reason="pre_fvg_window_already_closed",
            target_evaluation_time=target_evaluation,
            processed_at=observed,
        )
    visible_resolution_cutoff = min(target_evaluation, observed)
    relevant_rows: list[CandleLike] = []
    for index, bar in enumerate(lower_bars):
        opened, closed = _lower_bar_clock(bar, f"lower_bars[{index}]")
        if c_open <= opened and closed <= visible_resolution_cutoff:
            relevant_rows.append(bar)
    relevant_lower_bars = tuple(relevant_rows)
    if relevant_lower_bars:
        lower_timeframe = _required_candle_attribute(relevant_lower_bars[0], "relevant_lower_bars[0]", "timeframe")
        if type(lower_timeframe) is not str:
            raise TypeError("relevant lower bar timeframe must be a canonical string")
        for lower_bar in relevant_lower_bars:
            _validate_candle(lower_bar, "lower_bar")
            if lower_bar.symbol != b.symbol or lower_bar.timeframe != lower_timeframe:
                raise ValueError("lower_timeframe_series_contract_mismatch")
        lower_seconds = timeframe_seconds(lower_timeframe)
        target_seconds = timeframe_seconds(b.timeframe)
        seconds_to_cutoff = int((target_evaluation - c_open).total_seconds())
        if lower_seconds >= target_seconds or target_seconds % lower_seconds:
            return FvgPreEvaluation(
                status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
                reason="lower_timeframe_resolution_incompatible",
                target_evaluation_time=target_evaluation,
                processed_at=observed,
            )
        if seconds_to_cutoff % lower_seconds:
            return FvgPreEvaluation(
                status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
                reason="lower_timeframe_resolution_cannot_reach_evaluation_offset",
                target_evaluation_time=target_evaluation,
                processed_at=observed,
            )
    if observed < target_evaluation:
        return FvgPreEvaluation(
            status=FvgPreStatus.NOT_DUE,
            reason="pre_fvg_evaluation_not_due",
            target_evaluation_time=target_evaluation,
            processed_at=observed,
        )
    try:
        c = aggregate_provisional_c(
            lower_bars=lower_bars,
            symbol=b.symbol,
            target_timeframe=b.timeframe,
            target_open_time=c_open,
            cutoff_time=target_evaluation,
        )
    except ValueError as exc:
        return FvgPreEvaluation(
            status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            reason=str(exc),
            target_evaluation_time=target_evaluation,
            processed_at=observed,
        )

    a_high, a_low = as_decimal(a.high), as_decimal(a.low)
    direction: FvgDirection | None = None
    lower: Decimal | None = None
    upper: Decimal | None = None
    if c.low > a_high and config.direction_filter is not FvgDirectionFilter.BEARISH:
        direction, lower, upper = FvgDirection.BULLISH, a_high, c.low
    elif c.high < a_low and config.direction_filter is not FvgDirectionFilter.BULLISH:
        direction, lower, upper = FvgDirection.BEARISH, c.high, a_low
    if direction is None or lower is None or upper is None:
        return FvgPreEvaluation(
            status=FvgPreStatus.REJECTED,
            reason="pre_fvg_geometry_not_present_at_cutoff",
            target_evaluation_time=target_evaluation,
            processed_at=observed,
        )

    internal_zone = FvgZone(
        zone_id=make_pre_candidate_id(
            symbol=b.symbol,
            timeframe=b.timeframe,
            direction=direction,
            source_c_open_time=c_open,
            evaluated_at=target_evaluation,
            lower=lower,
            upper=upper,
        ),
        symbol=b.symbol,
        timeframe=b.timeframe,
        direction=direction,
        lower=lower,
        upper=upper,
        created_at=c.close_time,
        source_a_open_time=as_utc(a.timestamp),
        source_b_open_time=as_utc(b.timestamp),
        source_c_open_time=c_open,
    )
    volume_trace = (
        evaluate_volume_filter(
            a=a,
            b=b,
            c=c,
            previous_bars=tuple(previous_target_bars),
            config=config,
        )
        if _pre_filter_applies(config.volume_filter_target)
        else _skipped("volume", config.volume_filter_target)
    )
    effective_reference = as_decimal(c.close if reference_price is None else reference_price)
    if effective_reference <= 0:
        raise ValueError("reference_price must be positive")
    try:
        metrics = _calculate_metrics_validated(
            zone=internal_zone,
            a=a,
            b=b,
            c=c,
            history=tuple(previous_target_bars) + (a, b, c),
            reference_price=effective_reference,
            tick_size=config.tick_size,
            atr_period=config.atr_period,
            average_range_period=config.average_range_period,
            volume_ratio=volume_trace.observed,
        )
    except ValueError as exc:
        if str(exc) != "metric arithmetic exceeds supported decimal range":
            raise
        return FvgPreEvaluation(
            status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            reason=str(exc),
            target_evaluation_time=target_evaluation,
            processed_at=observed,
        )
    traces = (
        evaluate_gap_size_filter(metrics, config),
        evaluate_price_filter(internal_zone, config, reference_price=effective_reference)
        if _pre_filter_applies(config.price_filter_target)
        else _skipped("price", config.price_filter_target),
        volume_trace,
    )
    failed = next((trace for trace in traces if trace.decision is FvgFilterDecision.FAILED), None)
    if failed is not None:
        return FvgPreEvaluation(
            status=FvgPreStatus.REJECTED,
            reason=failed.reason,
            target_evaluation_time=target_evaluation,
            processed_at=observed,
            metrics=metrics,
            filters=traces,
        )
    unavailable = next((trace for trace in traces if trace.decision is FvgFilterDecision.UNAVAILABLE), None)
    if unavailable is not None:
        return FvgPreEvaluation(
            status=FvgPreStatus.UNAVAILABLE_WITH_CURRENT_DATA,
            reason=unavailable.reason,
            target_evaluation_time=target_evaluation,
            processed_at=observed,
            metrics=metrics,
            filters=traces,
        )

    candidate = FvgPreCandidate(
        candidate_id=internal_zone.zone_id,
        symbol=b.symbol,
        timeframe=b.timeframe,
        direction=direction,
        lower=lower,
        upper=upper,
        source_a_open_time=as_utc(a.timestamp),
        source_b_open_time=as_utc(b.timestamp),
        source_c_open_time=c_open,
        source_c_expected_close_time=target_close,
        evaluated_at=target_evaluation,
        processed_at=observed,
        remaining_seconds=int((target_close - target_evaluation).total_seconds()),
        metrics=metrics,
        filters=traces,
    )
    return FvgPreEvaluation(
        status=FvgPreStatus.CANDIDATE,
        candidate=candidate,
        reason="pre_fvg_candidate",
        target_evaluation_time=target_evaluation,
        processed_at=observed,
        metrics=metrics,
        filters=traces,
    )
