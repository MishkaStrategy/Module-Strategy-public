from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime
from decimal import Decimal

from .config import WaveAnalysisConfig, canonical_decimal
from .enums import (
    HypothesisPattern,
    HypothesisStatus,
    PivotKind,
    StructureState,
    SwingLabel,
    WaveClassification,
    WaveDirection,
)
from .models import BarRecord, PivotPoint, StructureSnapshot, TimeframeState, WaveHypothesis, WaveLeg, stable_id

ZERO = Decimal("0")
ONE = Decimal("1")
EPS = Decimal("0.00000001")
FIBONACCI = tuple(
    Decimal(v) for v in ("0.236", "0.382", "0.500", "0.618", "0.786", "1.000", "1.272", "1.618", "2.000", "2.618")
)


def clamp(value: Decimal, lower: Decimal = ZERO, upper: Decimal = ONE) -> Decimal:
    return min(max(value, lower), upper)


def mean(values: Iterable[Decimal]) -> Decimal:
    items = tuple(values)
    return sum(items, ZERO) / Decimal(len(items)) if items else ZERO


def bars_between(state: TimeframeState, start: PivotPoint, end: PivotPoint) -> list[BarRecord]:
    return [bar for bar in state.bars if start.pivot_time <= bar.timestamp <= end.pivot_time]


def current_segment_pivots(state: TimeframeState) -> list[PivotPoint]:
    """Return confirmed pivots that belong to the active post-gap segment."""
    return [pivot for pivot in state.confirmed_pivots if pivot.metadata.get("segment_id", 0) == state.segment_id]


def wave_segment_id(leg: WaveLeg) -> int:
    """Read a wave segment id while preserving compatibility with pre-segment checkpoints."""
    raw = leg.metadata.get("segment_id", 0)
    return raw if isinstance(raw, int) and not isinstance(raw, bool) and raw >= 0 else -1


def current_segment_waves(state: TimeframeState) -> list[WaveLeg]:
    """Return wave legs that are safe to compare inside the active segment."""
    return [leg for leg in state.wave_legs if wave_segment_id(leg) == state.segment_id]


def previous_directional_leg(state: TimeframeState, direction: WaveDirection) -> WaveLeg | None:
    return next((leg for leg in reversed(current_segment_waves(state)) if leg.direction is direction), None)


def classify_wave(
    *,
    atr_size: Decimal,
    path_efficiency: Decimal,
    relative_volume: Decimal,
    amplitude: Decimal,
    previous_same: WaveLeg | None,
    previous_leg: WaveLeg | None,
    config: WaveAnalysisConfig,
) -> tuple[WaveClassification, Decimal, Decimal, tuple[str, ...], dict[str, object]]:
    atr_component = clamp(atr_size / max(config.impulse_atr_threshold, EPS))
    efficiency_component = clamp(path_efficiency / max(config.impulse_efficiency_threshold, EPS))
    volume_component = clamp(relative_volume / Decimal("1.5")) if relative_volume > 0 else Decimal("0.5")
    impulse_score = mean((atr_component, efficiency_component, volume_component))
    ratio_previous = (
        None
        if previous_leg is None or abs(previous_leg.price_change) == 0
        else amplitude / abs(previous_leg.price_change)
    )
    ratio_same = (
        None
        if previous_same is None or abs(previous_same.price_change) == 0
        else amplitude / abs(previous_same.price_change)
    )
    reasons: list[str] = []

    if atr_size >= config.expansion_atr_threshold and path_efficiency >= config.impulse_efficiency_threshold:
        classification = WaveClassification.EXPANSION
        score = clamp(mean((atr_size / config.expansion_atr_threshold, path_efficiency, volume_component)))
        reasons.extend(("atr_expansion", "efficient_path"))
    elif atr_size >= config.impulse_atr_threshold and path_efficiency <= config.exhaustion_efficiency_threshold:
        classification = WaveClassification.EXHAUSTION
        score = clamp(mean((atr_size / config.impulse_atr_threshold, ONE - path_efficiency)))
        reasons.extend(("large_amplitude", "inefficient_path"))
    elif path_efficiency <= config.range_efficiency_threshold and atr_size < config.impulse_atr_threshold:
        classification = WaveClassification.RANGE_LEG
        score = clamp(ONE - path_efficiency)
        reasons.extend(("low_path_efficiency", "sub_impulse_amplitude"))
    elif ratio_same is not None and ratio_same <= Decimal("0.618"):
        classification = WaveClassification.CONTRACTION
        score = clamp(ONE - ratio_same / Decimal("0.618"))
        reasons.append("smaller_than_previous_same_direction")
    elif impulse_score >= Decimal("0.65"):
        classification = WaveClassification.IMPULSE
        score = impulse_score
        reasons.extend(("multi_factor_impulse_score", "amplitude_efficiency_volume"))
    elif ratio_previous is not None and ratio_previous <= Decimal("0.786"):
        classification = WaveClassification.CORRECTION
        score = clamp(ONE - ratio_previous / Decimal("1.25"))
        reasons.extend(("retraces_previous_leg", "reduced_amplitude"))
    else:
        classification = WaveClassification.UNCLASSIFIED
        score = clamp(impulse_score)
        reasons.append("insufficient_classification_evidence")

    confidence = clamp(Decimal("0.35") + abs(score - Decimal("0.5")))
    inputs: dict[str, object] = {
        "atr_normalized_size": canonical_decimal(atr_size),
        "path_efficiency": canonical_decimal(path_efficiency),
        "relative_volume": canonical_decimal(relative_volume),
        "impulse_score": canonical_decimal(impulse_score),
        "ratio_to_previous_leg": None if ratio_previous is None else canonical_decimal(ratio_previous),
        "ratio_to_previous_same_direction": None if ratio_same is None else canonical_decimal(ratio_same),
    }
    return classification, score, confidence, tuple(reasons), inputs


def build_wave_leg(state: TimeframeState, end_pivot: PivotPoint, config: WaveAnalysisConfig) -> WaveLeg | None:
    if len(state.confirmed_pivots) < 2:
        return None
    start = state.confirmed_pivots[-2]
    if start.pivot_id == end_pivot.pivot_id or start.kind is end_pivot.kind:
        return None
    if start.metadata.get("segment_id") != end_pivot.metadata.get("segment_id"):
        return None
    if any(existing.end_pivot_id == end_pivot.pivot_id for existing in state.wave_legs):
        return None

    direction = WaveDirection.UP if end_pivot.price > start.price else WaveDirection.DOWN
    change = end_pivot.price - start.price
    amplitude = abs(change)
    pct = change / start.price if start.price else ZERO
    duration_bars = max(1, end_pivot.bar_index - start.bar_index)
    duration_seconds = max(1, int((end_pivot.pivot_time - start.pivot_time).total_seconds()))
    bars = bars_between(state, start, end_pivot) or state.bars[-1:]
    wave_range = max(bar.high for bar in bars) - min(bar.low for bar in bars)
    volume = sum((bar.volume for bar in bars), ZERO)
    avg_volume = volume / Decimal(len(bars))
    prior = [bar.volume for bar in state.bars if bar.timestamp < start.pivot_time]
    baseline_volume = mean(prior[-duration_bars:]) if prior else ZERO
    relative_volume = avg_volume / baseline_volume if baseline_volume > 0 else ONE
    body_sum = sum((abs(bar.close - bar.open) for bar in bars), ZERO)
    range_sum = sum((bar.high - bar.low for bar in bars), ZERO)
    body_efficiency = body_sum / range_sum if range_sum > 0 else ZERO
    path_sum = sum((abs(bars[index].close - bars[index - 1].close) for index in range(1, len(bars))), ZERO)
    path_efficiency = clamp(amplitude / path_sum) if path_sum > 0 else (ONE if amplitude else ZERO)
    if direction is WaveDirection.UP:
        mfe = max(ZERO, max((bar.high - start.price for bar in bars), default=ZERO))
        mae = max(ZERO, max((start.price - bar.low for bar in bars), default=ZERO))
    else:
        mfe = max(ZERO, max((start.price - bar.low for bar in bars), default=ZERO))
        mae = max(ZERO, max((bar.high - start.price for bar in bars), default=ZERO))
    slope = change / Decimal(duration_bars)
    velocity = change / Decimal(duration_seconds)
    previous_same = previous_directional_leg(state, direction)
    acceleration = None if previous_same is None else (velocity - previous_same.velocity) / Decimal(duration_seconds)
    atr_reference = state.atr or mean(state.atr_values)
    atr_size = amplitude / atr_reference if atr_reference and atr_reference > 0 else ZERO
    segment_legs = current_segment_waves(state)
    previous_leg = segment_legs[-1] if segment_legs else None
    classification, score, confidence, reasons, inputs = classify_wave(
        atr_size=atr_size,
        path_efficiency=path_efficiency,
        relative_volume=relative_volume,
        amplitude=amplitude,
        previous_same=previous_same,
        previous_leg=previous_leg,
        config=config,
    )
    path_truncated = bool(bars and bars[0].timestamp > start.pivot_time)
    if path_truncated:
        confidence = confidence * Decimal("0.5")
        reasons = (*reasons, "path_truncated_metrics")
        inputs["path_truncated"] = True
    identity = {
        "symbol": state.symbol,
        "timeframe": state.timeframe,
        "start": start.pivot_id,
        "end": end_pivot.pivot_id,
        "config": config.signature,
    }
    leg = WaveLeg(
        wave_id=stable_id("wave", identity),
        symbol=state.symbol or "UNKNOWN",
        timeframe=state.timeframe,
        direction=direction,
        start_pivot_id=start.pivot_id,
        end_pivot_id=end_pivot.pivot_id,
        start_time=start.pivot_time,
        end_time=end_pivot.pivot_time,
        confirmed_at=end_pivot.confirmed_at,
        start_price=start.price,
        end_price=end_pivot.price,
        price_change=change,
        price_change_pct=pct,
        duration_bars=duration_bars,
        duration_seconds=duration_seconds,
        range=wave_range,
        atr_normalized_size=atr_size,
        volume=volume,
        relative_volume=relative_volume,
        body_efficiency=body_efficiency,
        path_efficiency=path_efficiency,
        maximum_adverse_excursion=mae,
        maximum_favorable_excursion=mfe,
        slope=slope,
        velocity=velocity,
        acceleration=acceleration,
        classification=classification,
        classification_score=score,
        confidence=confidence,
        status="confirmed",
        reason_codes=reasons,
        feature_inputs=inputs,
        config_fingerprint=config.signature,
        metadata={"path_truncated": path_truncated, "segment_id": state.segment_id},
    )
    state.wave_legs.append(leg)
    if len(state.wave_legs) > config.maximum_retained_waves:
        del state.wave_legs[: len(state.wave_legs) - config.maximum_retained_waves]
    return leg


def relative_relation(current: Decimal, previous: Decimal, tolerance: Decimal) -> int:
    basis = max(abs(current), abs(previous), EPS)
    if abs(current - previous) <= basis * tolerance:
        return 0
    return 1 if current > previous else -1


def external_structure(highs: list[PivotPoint], lows: list[PivotPoint], config: WaveAnalysisConfig) -> StructureState:
    if len(highs) < 2 or len(lows) < 2:
        return StructureState.UNDEFINED
    high = relative_relation(highs[-1].price, highs[-min(3, len(highs))].price, config.equal_level_tolerance)
    low = relative_relation(lows[-1].price, lows[-min(3, len(lows))].price, config.equal_level_tolerance)
    if high == 1 and low == 1:
        return StructureState.BULLISH
    if high == -1 and low == -1:
        return StructureState.BEARISH
    if high == 0 and low == 0:
        return StructureState.RANGE
    return StructureState.TRANSITION


def build_structure(state: TimeframeState, config: WaveAnalysisConfig, as_of: datetime) -> StructureSnapshot:
    segment_pivots = current_segment_pivots(state)
    highs = [pivot for pivot in segment_pivots if pivot.kind is PivotKind.HIGH]
    lows = [pivot for pivot in segment_pivots if pivot.kind is PivotKind.LOW]
    latest = segment_pivots[-1] if segment_pivots else None
    latest_label = SwingLabel.UNDEFINED
    high_relation = (
        relative_relation(highs[-1].price, highs[-2].price, config.equal_level_tolerance) if len(highs) >= 2 else None
    )
    low_relation = (
        relative_relation(lows[-1].price, lows[-2].price, config.equal_level_tolerance) if len(lows) >= 2 else None
    )
    if latest and latest.kind is PivotKind.HIGH and high_relation is not None:
        latest_label = {1: SwingLabel.HIGHER_HIGH, 0: SwingLabel.EQUAL_HIGH, -1: SwingLabel.LOWER_HIGH}[high_relation]
    if latest and latest.kind is PivotKind.LOW and low_relation is not None:
        latest_label = {1: SwingLabel.HIGHER_LOW, 0: SwingLabel.EQUAL_LOW, -1: SwingLabel.LOWER_LOW}[low_relation]

    reasons: list[str]
    if high_relation == 1 and low_relation == 1:
        structure, reasons = StructureState.BULLISH, ["higher_high_and_higher_low"]
    elif high_relation == -1 and low_relation == -1:
        structure, reasons = StructureState.BEARISH, ["lower_high_and_lower_low"]
    elif high_relation == 0 and low_relation == 0:
        structure, reasons, latest_label = StructureState.RANGE, ["equal_high_and_equal_low"], SwingLabel.RANGE
    elif high_relation is None or low_relation is None:
        structure, reasons = StructureState.UNDEFINED, ["insufficient_confirmed_swings"]
    else:
        structure, reasons = StructureState.TRANSITION, ["mixed_swing_relations"]

    previous_state = (
        state.structure.state
        if state.structure is not None and state.structure.segment_id == state.segment_id
        else StructureState.UNDEFINED
    )
    break_detected = previous_state in {StructureState.BULLISH, StructureState.BEARISH} and structure not in {
        previous_state,
        StructureState.UNDEFINED,
    }
    continuation = previous_state == structure and structure in {StructureState.BULLISH, StructureState.BEARISH}
    possible_reversal = break_detected and structure is StructureState.TRANSITION
    if possible_reversal:
        reasons.append("possible_reversal_after_structure_break")
    if structure is StructureState.BULLISH:
        invalidation = lows[-1].price if lows else None
    elif structure is StructureState.BEARISH:
        invalidation = highs[-1].price if highs else None
    else:
        invalidation = latest.price if latest else None
    relation_count = int(high_relation is not None) + int(low_relation is not None)
    confidence = clamp(
        Decimal("0.2")
        + Decimal("0.35") * Decimal(relation_count)
        + (
            Decimal("0.1")
            if structure in {StructureState.BULLISH, StructureState.BEARISH, StructureState.RANGE}
            else ZERO
        )
    )
    snapshot = StructureSnapshot(
        timeframe=state.timeframe,
        state=structure,
        latest_label=latest_label,
        internal_state=structure,
        external_state=external_structure(highs, lows, config),
        break_detected=break_detected,
        continuation=continuation,
        possible_reversal=possible_reversal,
        invalidation_price=invalidation,
        confidence=confidence,
        reason_codes=tuple(reasons),
        as_of=as_of,
        segment_id=state.segment_id,
    )
    state.structure = snapshot
    return snapshot


def retracement_metrics(state: TimeframeState) -> dict[str, object] | None:
    legs = current_segment_waves(state)
    if len(legs) < 2:
        return None
    impulse, correction = legs[-2], legs[-1]
    if impulse.direction is correction.direction or abs(impulse.price_change) == 0:
        return None
    ratio = abs(correction.price_change) / abs(impulse.price_change)
    time_ratio = Decimal(correction.duration_seconds) / Decimal(max(1, impulse.duration_seconds))
    volume_ratio = correction.volume / impulse.volume if impulse.volume > 0 else ZERO
    atr_ratio = (
        correction.atr_normalized_size / impulse.atr_normalized_size if impulse.atr_normalized_size > 0 else ZERO
    )
    overlap = min(abs(correction.price_change), abs(impulse.price_change)) / abs(impulse.price_change)
    nearest = min(FIBONACCI, key=lambda level: abs(level - ratio))
    complexity = clamp(mean((ONE - correction.path_efficiency, clamp(time_ratio / Decimal("2")), clamp(overlap))))
    continuation = clamp(mean((ONE - clamp(ratio / Decimal("1.2")), impulse.path_efficiency, ONE - complexity)))
    return {
        "impulse_wave_id": impulse.wave_id,
        "correction_wave_id": correction.wave_id,
        "retracement_percentage": canonical_decimal(ratio * Decimal("100")),
        "retracement_ratio": canonical_decimal(ratio),
        "atr_normalized_retracement": canonical_decimal(atr_ratio),
        "time_retracement": canonical_decimal(time_ratio),
        "price_retracement": canonical_decimal(abs(correction.price_change)),
        "volume_retracement": canonical_decimal(volume_ratio),
        "overlap": canonical_decimal(overlap),
        "correction_complexity": canonical_decimal(complexity),
        "correction_efficiency": canonical_decimal(correction.path_efficiency),
        "continuation_probability_score": canonical_decimal(continuation),
        "invalidation_level": canonical_decimal(impulse.start_price),
        "nearest_fibonacci_ratio": canonical_decimal(nearest),
        "fibonacci_distance": canonical_decimal(abs(nearest - ratio)),
    }


def extension_metrics(state: TimeframeState) -> dict[str, object] | None:
    legs = current_segment_waves(state)
    if not legs:
        return None
    latest = legs[-1]
    peers = [leg for leg in legs if leg.direction is latest.direction]
    if len(peers) < 2 or abs(peers[-2].price_change) == 0:
        return None
    previous, current = peers[-2], peers[-1]
    ratio = abs(current.price_change) / abs(previous.price_change)
    nearest = min(FIBONACCI, key=lambda level: abs(level - ratio))
    return {
        "reference_wave_id": previous.wave_id,
        "wave_id": current.wave_id,
        "extension_ratio": canonical_decimal(ratio),
        "nearest_fibonacci_ratio": canonical_decimal(nearest),
        "fibonacci_distance": canonical_decimal(abs(nearest - ratio)),
    }


def alternating(legs: list[WaveLeg]) -> bool:
    return all(legs[index].direction is not legs[index - 1].direction for index in range(1, len(legs)))


def guideline_score(legs: list[WaveLeg]) -> tuple[Decimal, tuple[str, ...]]:
    if len(legs) < 5:
        return Decimal("0.25"), ()
    w1, w2, w3, w4, w5 = legs[-5:]
    r2 = abs(w2.price_change) / abs(w1.price_change) if w1.price_change else ZERO
    r4 = abs(w4.price_change) / abs(w3.price_change) if w3.price_change else ZERO
    fib2 = min(abs(level - r2) for level in FIBONACCI[:5])
    fib4 = min(abs(level - r4) for level in FIBONACCI[:5])
    extension = abs(w3.price_change) / abs(w1.price_change) if w1.price_change else ZERO
    scores = (
        ONE - clamp((fib2 + fib4) / Decimal("0.5")),
        clamp(abs(r2 - r4) / Decimal("0.5")),
        clamp(extension / Decimal("1.618")),
        clamp(mean((w1.path_efficiency, w3.path_efficiency, w5.path_efficiency))),
    )
    return mean(scores), (
        "fibonacci_relationships_scored",
        "wave_2_4_alternation_scored",
        "wave_3_extension_scored",
        "momentum_continuity_scored",
    )


def _make_hypothesis(
    *,
    pattern: HypothesisPattern,
    direction: WaveDirection,
    state: TimeframeState,
    legs: list[WaveLeg],
    labels: dict[str, str],
    as_of: datetime,
    confidence: Decimal,
    rule_score: Decimal,
    guideline: Decimal,
    invalidation: Decimal,
    status: HypothesisStatus,
    reasons: tuple[str, ...],
    config: WaveAnalysisConfig,
) -> WaveHypothesis:
    identity = {
        "pattern": pattern.value,
        "waves": [leg.wave_id for leg in legs],
        "as_of": as_of.isoformat(),
        "config": config.signature,
    }
    return WaveHypothesis(
        hypothesis_id=stable_id("hypothesis", identity),
        pattern_type=pattern,
        direction=direction,
        degree=state.timeframe,
        wave_labels=labels,
        start_time=legs[0].start_time,
        end_time=legs[-1].end_time,
        confirmed_at=as_of,
        confidence=confidence,
        rule_score=rule_score,
        guideline_score=guideline,
        invalidation_price=invalidation,
        alternative_rank=0,
        status=status,
        reason_codes=reasons,
        supporting_wave_ids=tuple(leg.wave_id for leg in legs),
        metadata={"elliott_is_probabilistic": True, "segment_id": state.segment_id},
    )


def build_hypotheses(state: TimeframeState, config: WaveAnalysisConfig, as_of: datetime) -> list[WaveHypothesis]:
    if not config.elliott_enabled:
        state.hypotheses = []
        return []
    legs = current_segment_waves(state)
    candidates: list[WaveHypothesis] = []
    if len(legs) >= 5:
        window = legs[-5:]
        if alternating(window) and window[0].direction is window[2].direction is window[4].direction:
            bullish = window[0].direction is WaveDirection.UP
            w1, w2, w3, w4, w5 = window
            hard_rules = (
                w2.end_price > w1.start_price if bullish else w2.end_price < w1.start_price,
                abs(w3.price_change) >= min(abs(w1.price_change), abs(w5.price_change)),
                w4.end_price > w1.end_price if bullish else w4.end_price < w1.end_price,
            )
            if all(hard_rules):
                guide, guide_reasons = guideline_score(window)
                pattern = HypothesisPattern.IMPULSE_12345
                extension = abs(w3.price_change) / abs(w1.price_change) if w1.price_change else ZERO
                if extension >= Decimal("1.618"):
                    pattern = HypothesisPattern.POSSIBLE_EXTENSION
                truncated = w5.end_price <= w3.end_price if bullish else w5.end_price >= w3.end_price
                if truncated:
                    pattern = HypothesisPattern.TRUNCATED_FIFTH
                candidates.append(
                    _make_hypothesis(
                        pattern=pattern,
                        direction=w1.direction,
                        state=state,
                        legs=window,
                        labels={str(index + 1): leg.wave_id for index, leg in enumerate(window)},
                        as_of=as_of,
                        confidence=clamp(Decimal("0.65") + guide * Decimal("0.35")),
                        rule_score=ONE,
                        guideline=guide,
                        invalidation=w1.start_price,
                        status=HypothesisStatus.ACTIVE,
                        reasons=(
                            "strict_temporal_order",
                            "alternating_directions",
                            "wave_2_does_not_cross_wave_1_origin",
                            "wave_3_not_shortest",
                            "wave_4_no_overlap",
                            *guide_reasons,
                        ),
                        config=config,
                    )
                )
            else:
                # A diagonal-like interpretation is permitted only as a lower-confidence alternative.
                guide = clamp(mean(leg.path_efficiency for leg in window))
                candidates.append(
                    _make_hypothesis(
                        pattern=HypothesisPattern.DIAGONAL_LIKE,
                        direction=window[0].direction,
                        state=state,
                        legs=window,
                        labels={str(index + 1): leg.wave_id for index, leg in enumerate(window)},
                        as_of=as_of,
                        confidence=clamp(Decimal("0.30") + guide * Decimal("0.25")),
                        rule_score=Decimal("0.4"),
                        guideline=guide,
                        invalidation=window[0].start_price,
                        status=HypothesisStatus.INCOMPLETE,
                        reasons=("ordinary_impulse_hard_rule_failed", "diagonal_like_overlap_allowed"),
                        config=config,
                    )
                )
    if len(legs) >= 3:
        window3 = legs[-3:]
        if alternating(window3) and window3[0].direction is window3[2].direction:
            a, b, c = window3
            b_ratio = abs(b.price_change) / abs(a.price_change) if a.price_change else Decimal("10")
            if b_ratio < Decimal("1.272"):
                guide = clamp(mean((a.path_efficiency, c.path_efficiency, ONE - clamp(b_ratio / Decimal("1.272")))))
                pattern = (
                    HypothesisPattern.COMPLEX_CORRECTION
                    if len(legs) >= 7 and alternating(legs[-7:])
                    else HypothesisPattern.CORRECTION_ABC
                )
                candidates.append(
                    _make_hypothesis(
                        pattern=pattern,
                        direction=a.direction,
                        state=state,
                        legs=window3,
                        labels={"A": a.wave_id, "B": b.wave_id, "C": c.wave_id},
                        as_of=as_of,
                        confidence=clamp(Decimal("0.45") + guide * Decimal("0.4")),
                        rule_score=ONE,
                        guideline=guide,
                        invalidation=a.start_price,
                        status=HypothesisStatus.ACTIVE,
                        reasons=("alternating_abc", "b_does_not_excessively_retrace_a", "probabilistic_guidelines"),
                        config=config,
                    )
                )
    if not candidates and 2 <= len(legs) < 5:
        window = legs[-min(4, len(legs)) :]
        candidates.append(
            _make_hypothesis(
                pattern=HypothesisPattern.INCOMPLETE,
                direction=window[0].direction,
                state=state,
                legs=window,
                labels={str(index + 1): leg.wave_id for index, leg in enumerate(window)},
                as_of=as_of,
                confidence=Decimal("0.35"),
                rule_score=Decimal("0.5"),
                guideline=Decimal("0.25"),
                invalidation=window[0].start_price,
                status=HypothesisStatus.INCOMPLETE,
                reasons=("insufficient_confirmed_legs_for_complete_pattern",),
                config=config,
            )
        )

    candidates = [candidate for candidate in candidates if candidate.confidence >= config.hypothesis_min_confidence]
    preserved_invalidated = [
        item
        for item in state.hypotheses
        if item.status is HypothesisStatus.INVALIDATED and item.metadata.get("segment_id", 0) == state.segment_id
    ]
    candidates.sort(key=lambda item: (-item.confidence, item.hypothesis_id))
    preserved_invalidated.sort(
        key=lambda item: (item.metadata.get("invalidated_at", ""), item.hypothesis_id), reverse=True
    )
    if preserved_invalidated and config.maximum_hypotheses > 1:
        selected = candidates[: config.maximum_hypotheses - 1] + preserved_invalidated[:1]
    elif preserved_invalidated and not candidates:
        selected = preserved_invalidated[: config.maximum_hypotheses]
    else:
        selected = candidates[: config.maximum_hypotheses]
    limited: list[WaveHypothesis] = []
    for rank, item in enumerate(selected, 1):
        limited.append(
            WaveHypothesis(
                hypothesis_id=item.hypothesis_id,
                pattern_type=item.pattern_type,
                direction=item.direction,
                degree=item.degree,
                wave_labels=item.wave_labels,
                start_time=item.start_time,
                end_time=item.end_time,
                confirmed_at=item.confirmed_at,
                confidence=item.confidence,
                rule_score=item.rule_score,
                guideline_score=item.guideline_score,
                invalidation_price=item.invalidation_price,
                alternative_rank=rank,
                status=item.status,
                reason_codes=item.reason_codes,
                supporting_wave_ids=item.supporting_wave_ids,
                metadata=item.metadata,
            )
        )
    state.hypotheses = limited
    return limited


def update_hypothesis_invalidation(state: TimeframeState, bar: BarRecord) -> bool:
    """Mark active/incomplete hypotheses invalidated using only the current completed bar."""
    changed = False
    updated: list[WaveHypothesis] = []
    for item in state.hypotheses:
        if item.status is HypothesisStatus.INVALIDATED:
            updated.append(item)
            continue
        breached = (
            bar.low <= item.invalidation_price
            if item.direction is WaveDirection.UP
            else bar.high >= item.invalidation_price
        )
        if not breached:
            updated.append(item)
            continue
        metadata = dict(item.metadata)
        metadata["invalidated_at"] = bar.close_time.isoformat()
        updated.append(
            WaveHypothesis(
                hypothesis_id=item.hypothesis_id,
                pattern_type=item.pattern_type,
                direction=item.direction,
                degree=item.degree,
                wave_labels=item.wave_labels,
                start_time=item.start_time,
                end_time=item.end_time,
                confirmed_at=item.confirmed_at,
                confidence=ZERO,
                rule_score=item.rule_score,
                guideline_score=item.guideline_score,
                invalidation_price=item.invalidation_price,
                alternative_rank=item.alternative_rank,
                status=HypothesisStatus.INVALIDATED,
                reason_codes=(*item.reason_codes, "invalidation_price_breached"),
                supporting_wave_ids=item.supporting_wave_ids,
                metadata=metadata,
            )
        )
        changed = True
    if changed:
        state.hypotheses = updated
    return changed
