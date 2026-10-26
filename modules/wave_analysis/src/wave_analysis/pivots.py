from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from modular_core import Candle

from .config import WaveAnalysisConfig, canonical_decimal
from .enums import PivotKind, PivotMethod, PivotStatus, PriceBasis, TiePolicy, WaveDirection
from .models import BarRecord, PivotPoint, ProvisionalPivot, TimeframeState, stable_id

_ZERO = Decimal("0")
_ONE = Decimal("1")


def decimal_from_number(value: int | float | str | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _float_hex(value: int | float) -> str:
    number = float(value)
    return (0.0 if number == 0.0 else number).hex()


def candle_fingerprint(candle: Candle) -> str:
    return "|".join(
        (
            candle.timestamp.isoformat(),
            candle.symbol,
            candle.timeframe,
            _float_hex(candle.open),
            _float_hex(candle.high),
            _float_hex(candle.low),
            _float_hex(candle.close),
            _float_hex(candle.volume),
        )
    )


def to_bar(candle: Candle) -> BarRecord:
    return BarRecord(
        candle.timestamp,
        candle.close_time,
        decimal_from_number(candle.open),
        decimal_from_number(candle.high),
        decimal_from_number(candle.low),
        decimal_from_number(candle.close),
        decimal_from_number(candle.volume),
    )


def update_atr(state: TimeframeState, bar: BarRecord, config: WaveAnalysisConfig) -> None:
    pc = state.previous_close
    tr = bar.high - bar.low if pc is None else max(bar.high - bar.low, abs(bar.high - pc), abs(bar.low - pc))
    state.previous_close = bar.close
    if state.atr is None:
        state.tr_seed.append(tr)
        if len(state.tr_seed) == config.atr_period:
            state.atr = sum(state.tr_seed, _ZERO) / Decimal(config.atr_period)
    else:
        p = Decimal(config.atr_period)
        state.atr = ((state.atr * (p - _ONE)) + tr) / p
    if state.atr is not None:
        state.atr_values.append(state.atr)
        if len(state.atr_values) > config.maximum_retained_atr_values:
            del state.atr_values[: len(state.atr_values) - config.maximum_retained_atr_values]


def append_bar(state: TimeframeState, bar: BarRecord, config: WaveAnalysisConfig) -> int:
    state.bars.append(bar)
    state.processed_bars += 1
    if len(state.bars) > config.maximum_retained_bars:
        remove = len(state.bars) - config.maximum_retained_bars
        del state.bars[:remove]
        state.bar_offset += remove
    return state.bar_offset + len(state.bars) - 1


def _tie_passes(values: list[Decimal], candidate_index: int, policy: TiePolicy, *, high: bool) -> bool:
    extreme = max(values) if high else min(values)
    matches = [i for i, v in enumerate(values) if v == extreme]
    if values[candidate_index] != extreme:
        return False
    if policy is TiePolicy.REJECT:
        return len(matches) == 1
    if policy is TiePolicy.EARLIEST:
        return candidate_index == matches[0]
    return candidate_index == matches[-1]


def _last_segment_pivot(state: TimeframeState) -> PivotPoint | None:
    return next(
        (
            pivot
            for pivot in reversed(state.confirmed_pivots)
            if pivot.metadata.get("segment_id", 0) == state.segment_id
        ),
        None,
    )


def _eligible_against_last(
    state: TimeframeState, *, price: Decimal, absolute_index: int, config: WaveAnalysisConfig
) -> bool:
    last = _last_segment_pivot(state)
    if last is None:
        return True
    return (
        absolute_index - last.bar_index >= config.minimum_leg_bars
        and abs(price - last.price) >= config.minimum_price_movement
    )


def _confirmed_pivot(
    state: TimeframeState,
    config: WaveAnalysisConfig,
    *,
    kind: PivotKind,
    pivot_time: datetime,
    confirmed_at: datetime,
    price: Decimal,
    bar_index: int,
    reason_codes: Iterable[str],
) -> PivotPoint:
    identity = {
        "symbol": state.symbol,
        "timeframe": state.timeframe,
        "kind": kind.value,
        "pivot_time": pivot_time.isoformat(),
        "confirmed_at": confirmed_at.isoformat(),
        "price": canonical_decimal(price),
        "config": config.signature,
    }
    return PivotPoint(
        stable_id("pivot", identity),
        state.symbol or "UNKNOWN",
        state.timeframe,
        kind,
        pivot_time,
        confirmed_at,
        price,
        bar_index,
        PivotStatus.CONFIRMED,
        0,
        tuple(reason_codes),
        config.signature,
        {"segment_id": state.segment_id},
    )


def _set_provisional(
    state: TimeframeState,
    config: WaveAnalysisConfig,
    *,
    kind: PivotKind,
    pivot_time: datetime,
    observed_at: datetime,
    price: Decimal,
    bar_index: int,
    threshold: Decimal,
) -> bool:
    ex = state.provisional
    if (
        ex is not None
        and ex.kind is kind
        and ex.pivot_time == pivot_time
        and ex.price == price
        and ex.threshold == threshold
    ):
        return False
    state.revision_counter += 1
    base = {
        "symbol": state.symbol,
        "timeframe": state.timeframe,
        "kind": kind.value,
        "pivot_time": pivot_time.isoformat(),
        "config": config.signature,
    }
    rev = {
        **base,
        "observed_at": observed_at.isoformat(),
        "price": canonical_decimal(price),
        "threshold": canonical_decimal(threshold),
        "revision": state.revision_counter,
    }
    state.provisional = ProvisionalPivot(
        stable_id("provisional", base),
        stable_id("pivot-revision", rev),
        state.symbol or "UNKNOWN",
        state.timeframe,
        kind,
        pivot_time,
        observed_at,
        price,
        bar_index,
        state.revision_counter,
        threshold,
        PivotStatus.PROVISIONAL,
        config.signature,
    )
    return True


def _append_confirmed(state: TimeframeState, pivot: PivotPoint, config: WaveAnalysisConfig) -> bool:
    if any(x.pivot_id == pivot.pivot_id for x in state.confirmed_pivots):
        return False
    state.confirmed_pivots.append(pivot)
    if len(state.confirmed_pivots) > config.maximum_retained_pivots:
        del state.confirmed_pivots[: len(state.confirmed_pivots) - config.maximum_retained_pivots]
    return True


def _update_fractal_provisional(state: TimeframeState, config: WaveAnalysisConfig, current_bar: BarRecord) -> bool:
    lookback = min(len(state.bars), config.left_bars + config.right_bars + 1)
    if lookback == 0:
        return False
    recent = state.bars[-lookback:]
    last_segment_pivot = _last_segment_pivot(state)
    if last_segment_pivot is not None:
        next_kind = PivotKind.LOW if last_segment_pivot.kind is PivotKind.HIGH else PivotKind.HIGH
    else:
        next_kind = (
            PivotKind.HIGH
            if current_bar.high - current_bar.close >= current_bar.close - current_bar.low
            else PivotKind.LOW
        )
    if next_kind is PivotKind.HIGH:
        extreme = max(recent, key=lambda x: (x.high, -x.timestamp.timestamp()))
        price = extreme.high
    else:
        extreme = min(recent, key=lambda x: (x.low, x.timestamp.timestamp()))
        price = extreme.low
    absolute = state.bar_offset + state.bars.index(extreme)
    return _set_provisional(
        state,
        config,
        kind=next_kind,
        pivot_time=extreme.timestamp,
        observed_at=current_bar.close_time,
        price=price,
        bar_index=absolute,
        threshold=_ZERO,
    )


def update_fractal_pivots(
    state: TimeframeState, config: WaveAnalysisConfig, current_bar: BarRecord
) -> tuple[tuple[PivotPoint, ...], bool]:
    confirmed = []
    prov = _update_fractal_provisional(state, config, current_bar)
    required = config.left_bars + config.right_bars + 1
    if len(state.bars) < required:
        return (), prov
    candidate_local = len(state.bars) - 1 - config.right_bars
    start = candidate_local - config.left_bars
    end = candidate_local + config.right_bars + 1
    if start < 0:
        return (), prov
    window = state.bars[start:end]
    center = config.left_bars
    candidate = window[center]
    absolute = state.bar_offset + candidate_local
    high_ok = _tie_passes([x.high for x in window], center, config.tie_policy, high=True)
    low_ok = _tie_passes([x.low for x in window], center, config.tie_policy, high=False)
    if high_ok and low_ok:
        last = _last_segment_pivot(state)
        if last is not None:
            hd = abs(candidate.high - last.price)
            ld = abs(candidate.low - last.price)
            if hd > ld:
                low_ok = False
            elif ld > hd:
                high_ok = False
            else:
                high_ok = low_ok = False
        else:
            high_ok = low_ok = False
    for kind, price, passes, reason in (
        (PivotKind.HIGH, candidate.high, high_ok, "fractal_high_confirmed"),
        (PivotKind.LOW, candidate.low, low_ok, "fractal_low_confirmed"),
    ):
        if not passes or not _eligible_against_last(state, price=price, absolute_index=absolute, config=config):
            continue
        pivot = _confirmed_pivot(
            state,
            config,
            kind=kind,
            pivot_time=candidate.timestamp,
            confirmed_at=current_bar.close_time,
            price=price,
            bar_index=absolute,
            reason_codes=(reason, f"right_bars={config.right_bars}"),
        )
        if _append_confirmed(state, pivot, config):
            confirmed.append(pivot)
            if state.provisional is not None and state.provisional.kind is kind:
                state.provisional = None
                prov = True
    return tuple(confirmed), prov


def _mean(values: list[Decimal]) -> Decimal:
    return sum(values, _ZERO) / Decimal(len(values)) if values else _ZERO


def reversal_threshold(state: TimeframeState, config: WaveAnalysisConfig, reference_price: Decimal) -> Decimal | None:
    percent = abs(reference_price) * config.reversal_threshold
    if config.pivot_method is PivotMethod.PERCENTAGE_ZIGZAG:
        return max(percent, config.minimum_absolute_movement, config.minimum_price_movement)
    if state.atr is None:
        return None
    atr = min(max(state.atr * config.atr_multiplier, config.minimum_atr_threshold), config.maximum_atr_threshold)
    if config.pivot_method is PivotMethod.ATR_ZIGZAG:
        return max(atr, config.minimum_absolute_movement, config.minimum_price_movement)
    baseline = _mean(state.atr_values)
    ratio = state.atr / baseline if baseline > 0 else _ONE
    adjust = _ONE + config.hybrid_volatility_adjustment * max(ratio - _ONE, _ZERO)
    return max(percent, atr * adjust, config.minimum_absolute_movement, config.minimum_price_movement)


def _basis_down(bar: BarRecord, config: WaveAnalysisConfig) -> Decimal:
    return bar.low if config.price_basis is PriceBasis.HIGH_LOW else bar.close


def _basis_up(bar: BarRecord, config: WaveAnalysisConfig) -> Decimal:
    return bar.high if config.price_basis is PriceBasis.HIGH_LOW else bar.close


def _update_initial_extremes(
    state: TimeframeState, config: WaveAnalysisConfig, bar: BarRecord, absolute_index: int
) -> bool:
    changed = False
    if (
        state.initial_high is None
        or bar.high > state.initial_high[0]
        or (bar.high == state.initial_high[0] and config.tie_policy is TiePolicy.LATEST)
    ):
        state.initial_high = (bar.high, bar.timestamp, absolute_index)
        changed = True
    if (
        state.initial_low is None
        or bar.low < state.initial_low[0]
        or (bar.low == state.initial_low[0] and config.tie_policy is TiePolicy.LATEST)
    ):
        state.initial_low = (bar.low, bar.timestamp, absolute_index)
        changed = True
    high = state.initial_high
    low = state.initial_low
    if high[2] >= low[2]:
        kind = PivotKind.HIGH
        price, pivot_time, index = high
    else:
        kind = PivotKind.LOW
        price, pivot_time, index = low
    threshold = reversal_threshold(state, config, price)
    if threshold is None:
        return changed
    return changed | _set_provisional(
        state,
        config,
        kind=kind,
        pivot_time=pivot_time,
        observed_at=bar.close_time,
        price=price,
        bar_index=index,
        threshold=threshold,
    )


def _try_confirm_initial(
    state: TimeframeState, config: WaveAnalysisConfig, current_bar: BarRecord
) -> PivotPoint | None:
    high = state.initial_high
    low = state.initial_low
    if high is None or low is None:
        return None
    up = reversal_threshold(state, config, low[0])
    down = reversal_threshold(state, config, high[0])
    if up is None or down is None:
        return None
    upward = high[0] - low[0] >= up and high[2] - low[2] >= config.minimum_leg_bars
    downward = high[0] - low[0] >= down and low[2] - high[2] >= config.minimum_leg_bars
    if upward and downward:
        upward = high[2] > low[2]
        downward = not upward
    if upward:
        pivot = _confirmed_pivot(
            state,
            config,
            kind=PivotKind.LOW,
            pivot_time=low[1],
            confirmed_at=current_bar.close_time,
            price=low[0],
            bar_index=low[2],
            reason_codes=("zigzag_initial_reversal", config.pivot_method.value),
        )
        state.zigzag_direction = WaveDirection.UP
        state.provisional = None
        _set_provisional(
            state,
            config,
            kind=PivotKind.HIGH,
            pivot_time=high[1],
            observed_at=current_bar.close_time,
            price=high[0],
            bar_index=high[2],
            threshold=reversal_threshold(state, config, high[0]) or _ZERO,
        )
        return pivot
    if downward:
        pivot = _confirmed_pivot(
            state,
            config,
            kind=PivotKind.HIGH,
            pivot_time=high[1],
            confirmed_at=current_bar.close_time,
            price=high[0],
            bar_index=high[2],
            reason_codes=("zigzag_initial_reversal", config.pivot_method.value),
        )
        state.zigzag_direction = WaveDirection.DOWN
        state.provisional = None
        _set_provisional(
            state,
            config,
            kind=PivotKind.LOW,
            pivot_time=low[1],
            observed_at=current_bar.close_time,
            price=low[0],
            bar_index=low[2],
            threshold=reversal_threshold(state, config, low[0]) or _ZERO,
        )
        return pivot
    return None


def update_zigzag_pivots(
    state: TimeframeState, config: WaveAnalysisConfig, current_bar: BarRecord, absolute_index: int
) -> tuple[tuple[PivotPoint, ...], bool]:
    confirmed = []
    changed = False
    if state.zigzag_direction is None:
        changed |= _update_initial_extremes(state, config, current_bar, absolute_index)
        initial = _try_confirm_initial(state, config, current_bar)
        if initial is not None and _append_confirmed(state, initial, config):
            confirmed.append(initial)
            changed = True
        return tuple(confirmed), changed
    provisional = state.provisional
    if provisional is None:
        next_kind = PivotKind.HIGH if state.zigzag_direction is WaveDirection.UP else PivotKind.LOW
        price = current_bar.high if next_kind is PivotKind.HIGH else current_bar.low
        threshold = reversal_threshold(state, config, price)
        if threshold is None:
            return (), False
        changed |= _set_provisional(
            state,
            config,
            kind=next_kind,
            pivot_time=current_bar.timestamp,
            observed_at=current_bar.close_time,
            price=price,
            bar_index=absolute_index,
            threshold=threshold,
        )
        provisional = state.provisional
    assert provisional is not None
    if provisional.kind is PivotKind.HIGH:
        if current_bar.high > provisional.price or (
            current_bar.high == provisional.price and config.tie_policy is TiePolicy.LATEST
        ):
            threshold = reversal_threshold(state, config, current_bar.high)
            if threshold is not None:
                changed |= _set_provisional(
                    state,
                    config,
                    kind=PivotKind.HIGH,
                    pivot_time=current_bar.timestamp,
                    observed_at=current_bar.close_time,
                    price=current_bar.high,
                    bar_index=absolute_index,
                    threshold=threshold,
                )
                provisional = state.provisional
        assert provisional is not None
        if (
            absolute_index - provisional.bar_index >= config.minimum_leg_bars
            and provisional.price - _basis_down(current_bar, config) >= provisional.threshold
        ):
            pivot = _confirmed_pivot(
                state,
                config,
                kind=PivotKind.HIGH,
                pivot_time=provisional.pivot_time,
                confirmed_at=current_bar.close_time,
                price=provisional.price,
                bar_index=provisional.bar_index,
                reason_codes=("zigzag_reversal_confirmed", config.pivot_method.value),
            )
            if _append_confirmed(state, pivot, config):
                confirmed.append(pivot)
                state.zigzag_direction = WaveDirection.DOWN
                threshold = reversal_threshold(state, config, current_bar.low) or _ZERO
                state.provisional = None
                changed = True
                changed |= _set_provisional(
                    state,
                    config,
                    kind=PivotKind.LOW,
                    pivot_time=current_bar.timestamp,
                    observed_at=current_bar.close_time,
                    price=current_bar.low,
                    bar_index=absolute_index,
                    threshold=threshold,
                )
    else:
        if current_bar.low < provisional.price or (
            current_bar.low == provisional.price and config.tie_policy is TiePolicy.LATEST
        ):
            threshold = reversal_threshold(state, config, current_bar.low)
            if threshold is not None:
                changed |= _set_provisional(
                    state,
                    config,
                    kind=PivotKind.LOW,
                    pivot_time=current_bar.timestamp,
                    observed_at=current_bar.close_time,
                    price=current_bar.low,
                    bar_index=absolute_index,
                    threshold=threshold,
                )
                provisional = state.provisional
        assert provisional is not None
        if (
            absolute_index - provisional.bar_index >= config.minimum_leg_bars
            and _basis_up(current_bar, config) - provisional.price >= provisional.threshold
        ):
            pivot = _confirmed_pivot(
                state,
                config,
                kind=PivotKind.LOW,
                pivot_time=provisional.pivot_time,
                confirmed_at=current_bar.close_time,
                price=provisional.price,
                bar_index=provisional.bar_index,
                reason_codes=("zigzag_reversal_confirmed", config.pivot_method.value),
            )
            if _append_confirmed(state, pivot, config):
                confirmed.append(pivot)
                state.zigzag_direction = WaveDirection.UP
                threshold = reversal_threshold(state, config, current_bar.high) or _ZERO
                state.provisional = None
                changed = True
                changed |= _set_provisional(
                    state,
                    config,
                    kind=PivotKind.HIGH,
                    pivot_time=current_bar.timestamp,
                    observed_at=current_bar.close_time,
                    price=current_bar.high,
                    bar_index=absolute_index,
                    threshold=threshold,
                )
    return tuple(confirmed), changed


def update_pivots(
    state: TimeframeState, config: WaveAnalysisConfig, current_bar: BarRecord, absolute_index: int
) -> tuple[tuple[PivotPoint, ...], bool]:
    return (
        update_fractal_pivots(state, config, current_bar)
        if config.pivot_method is PivotMethod.FRACTAL
        else update_zigzag_pivots(state, config, current_bar, absolute_index)
    )
