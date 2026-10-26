from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from decimal import Decimal
from threading import RLock
from typing import cast

from modular_core import Candle, Feature, FeatureDelete, MarketView, ModuleDecision, StrategyModule

from .analysis import (
    build_hypotheses,
    build_structure,
    build_wave_leg,
    current_segment_pivots,
    current_segment_waves,
    extension_metrics,
    retracement_metrics,
    update_hypothesis_invalidation,
)
from .config import WaveAnalysisConfig, canonical_decimal, timeframe_seconds, to_decimal
from .contracts import (
    CONFIDENCE_FEATURE,
    CONFIRMED_PIVOTS_FEATURE,
    CORRECTION_SCORE_FEATURE,
    CURRENT_WAVE_FEATURE,
    EXTENSION_FEATURE,
    FEATURE_SCHEMA_VERSION,
    HYPOTHESES_FEATURE,
    IMPULSE_SCORE_FEATURE,
    INVALIDATION_FEATURE,
    MTF_CONTEXT_FEATURE,
    PRIMARY_HYPOTHESIS_FEATURE,
    PROVISIONAL_PIVOT_FEATURE,
    RETRACEMENT_FEATURE,
    STRUCTURE_FEATURE,
    WAVE_LEGS_FEATURE,
)
from .enums import LifecycleState, PivotMethod, PriceBasis, SkipReason, StructureState, TiePolicy, WaveClassification
from .exceptions import WaveAnalysisConfigurationError, WaveAnalysisDataError
from .manifest import MODULE_NAME, MODULE_VERSION, build_manifest
from .models import TimeframeState, stable_id
from .pivots import append_bar, candle_fingerprint, to_bar, update_atr, update_pivots
from .state import RuntimeState


class WaveAnalysisModule(StrategyModule):
    """Deterministic, non-repainting analytical wave-structure producer."""

    name = MODULE_NAME

    def __init__(
        self,
        *,
        timeframes: Sequence[str] = ("15m",),
        primary_timeframe: str = "15m",
        pivot_method: str | PivotMethod = PivotMethod.FRACTAL,
        left_bars: int = 3,
        right_bars: int = 3,
        tie_policy: str | TiePolicy = TiePolicy.EARLIEST,
        price_basis: str | PriceBasis = PriceBasis.HIGH_LOW,
        minimum_price_movement: int | float | str | Decimal = "0",
        minimum_leg_bars: int = 1,
        reversal_threshold: int | float | str | Decimal = "0.01",
        atr_period: int = 14,
        atr_multiplier: int | float | str | Decimal = "2",
        minimum_atr_threshold: int | float | str | Decimal = "0",
        maximum_atr_threshold: int | float | str | Decimal = "1000000000",
        minimum_absolute_movement: int | float | str | Decimal = "0",
        hybrid_volatility_adjustment: int | float | str | Decimal = "0.25",
        equal_level_tolerance: int | float | str | Decimal = "0.0005",
        impulse_atr_threshold: int | float | str | Decimal = "1.5",
        impulse_efficiency_threshold: int | float | str | Decimal = "0.55",
        expansion_atr_threshold: int | float | str | Decimal = "2.5",
        range_efficiency_threshold: int | float | str | Decimal = "0.25",
        exhaustion_efficiency_threshold: int | float | str | Decimal = "0.35",
        elliott_enabled: bool = False,
        maximum_hypotheses: int = 5,
        hypothesis_min_confidence: int | float | str | Decimal = "0.35",
        maximum_retained_bars: int = 4096,
        maximum_retained_pivots: int = 256,
        maximum_retained_waves: int = 255,
        maximum_retained_atr_values: int = 128,
        feature_snapshot_limit: int = 32,
        recovery_bars: int = 0,
        publish_provisional: bool = True,
        publish_diagnostics: bool = True,
        optional_market_regime_feature: str = "market_regime.current",
    ) -> None:
        if isinstance(timeframes, (str, bytes)):
            raise WaveAnalysisConfigurationError("timeframes must be a sequence of timeframe strings, not a scalar")
        self.config = WaveAnalysisConfig(
            timeframes=tuple(timeframes),
            primary_timeframe=primary_timeframe,
            pivot_method=pivot_method,  # type: ignore[arg-type]
            left_bars=left_bars,
            right_bars=right_bars,
            tie_policy=tie_policy,  # type: ignore[arg-type]
            price_basis=price_basis,  # type: ignore[arg-type]
            minimum_price_movement=to_decimal(minimum_price_movement, "minimum_price_movement"),
            minimum_leg_bars=minimum_leg_bars,
            reversal_threshold=to_decimal(reversal_threshold, "reversal_threshold"),
            atr_period=atr_period,
            atr_multiplier=to_decimal(atr_multiplier, "atr_multiplier"),
            minimum_atr_threshold=to_decimal(minimum_atr_threshold, "minimum_atr_threshold"),
            maximum_atr_threshold=to_decimal(maximum_atr_threshold, "maximum_atr_threshold"),
            minimum_absolute_movement=to_decimal(minimum_absolute_movement, "minimum_absolute_movement"),
            hybrid_volatility_adjustment=to_decimal(hybrid_volatility_adjustment, "hybrid_volatility_adjustment"),
            equal_level_tolerance=to_decimal(equal_level_tolerance, "equal_level_tolerance"),
            impulse_atr_threshold=to_decimal(impulse_atr_threshold, "impulse_atr_threshold"),
            impulse_efficiency_threshold=to_decimal(impulse_efficiency_threshold, "impulse_efficiency_threshold"),
            expansion_atr_threshold=to_decimal(expansion_atr_threshold, "expansion_atr_threshold"),
            range_efficiency_threshold=to_decimal(range_efficiency_threshold, "range_efficiency_threshold"),
            exhaustion_efficiency_threshold=to_decimal(
                exhaustion_efficiency_threshold, "exhaustion_efficiency_threshold"
            ),
            elliott_enabled=elliott_enabled,
            maximum_hypotheses=maximum_hypotheses,
            hypothesis_min_confidence=to_decimal(hypothesis_min_confidence, "hypothesis_min_confidence"),
            maximum_retained_bars=maximum_retained_bars,
            maximum_retained_pivots=maximum_retained_pivots,
            maximum_retained_waves=maximum_retained_waves,
            maximum_retained_atr_values=maximum_retained_atr_values,
            feature_snapshot_limit=feature_snapshot_limit,
            recovery_bars=recovery_bars,
            publish_provisional=publish_provisional,
            publish_diagnostics=publish_diagnostics,
            optional_market_regime_feature=optional_market_regime_feature,
        )
        self.parameters = self.config.as_parameters()
        self.manifest = build_manifest(self.config)
        self._lock = RLock()
        self._state = RuntimeState.empty(self.config)

    def on_start(self) -> None:
        with self._lock:
            self._state = RuntimeState.empty(self.config)

    def on_bar(self, view: MarketView) -> ModuleDecision:
        # Core normally invokes modules serially. The lock additionally makes
        # duplicate delivery and concurrent checkpoint reads atomic for paper/live
        # adapters without pretending to reorder distinct out-of-order events.
        with self._lock:
            return self._on_bar_unlocked(view)

    def _on_bar_unlocked(self, view: MarketView) -> ModuleDecision:
        if not view.base_bars:
            return self._skip(SkipReason.EMPTY_VIEW)
        base = view.current
        base_fingerprint = candle_fingerprint(base)
        if self._validate_base_event(base, base_fingerprint):
            return self._skip(SkipReason.DUPLICATE_BAR)
        self._prevalidate_targets(view, base)
        self._bind_symbol(base)
        self._remember_base(base, base_fingerprint)
        self._state.processed_base_events += 1

        if view.data_gap:
            gap_deletions = self._clear_persistent_feature_snapshots()
            self._state.gap_events += 1
            for timeframe, state in self._state.timeframes.items():
                self._mark_gap(state)
                target = base if base.timeframe == timeframe else view.current_for(timeframe)
                if target is not None and target.close_time <= base.close_time:
                    # The gap event itself is intentionally not analysed, but its
                    # latest completed target identity anchors the next continuity
                    # check and prevents a second synthetic gap segment.
                    state.last_open_time = target.timestamp
                    state.last_fingerprint = candle_fingerprint(target)
            return self._skip(
                SkipReason.DATA_GAP,
                feature_deletes=gap_deletions,
                state_digest=self._state.digest,
            )

        changes: dict[str, set[str]] = {}
        processed_targets = 0
        recovery_targets = 0
        for timeframe in self.config.timeframes:
            target = base if base.timeframe == timeframe else view.current_for(timeframe)
            if target is None or target.close_time > base.close_time:
                continue
            state = self._state.timeframes[timeframe]
            if target.timeframe != timeframe:
                raise WaveAnalysisDataError(
                    f"target timeframe {target.timeframe!r} does not match configured {timeframe!r}"
                )
            if target.symbol != base.symbol:
                raise WaveAnalysisDataError(
                    f"target symbol {target.symbol!r} does not match base symbol {base.symbol!r}"
                )
            target_fingerprint = candle_fingerprint(target)
            if self._target_is_duplicate(state, target, target_fingerprint):
                continue
            if state.last_open_time is not None:
                expected = timedelta(seconds=timeframe_seconds(timeframe))
                if target.timestamp - state.last_open_time != expected:
                    self._mark_gap(state)
            state.last_open_time = target.timestamp
            state.last_fingerprint = target_fingerprint
            processed_targets += 1
            bar = to_bar(target)
            absolute_index = append_bar(state, bar, self.config)
            update_atr(state, bar, self.config)

            recovering = state.gap_blocked
            recovery_complete = False
            if recovering:
                state.recovery_count += 1
                recovery_targets += 1
                recovery_complete = state.recovery_count >= self.config.effective_recovery_bars

            # Recovery bars must rebuild detector state point-in-time just like a fresh
            # module instance. Only external publication is suppressed until the
            # configured recovery window is complete.
            hypothesis_invalidated = update_hypothesis_invalidation(state, bar)
            confirmed, provisional_changed = update_pivots(state, self.config, bar, absolute_index)
            wave_changed = False
            for pivot in confirmed:
                state.lifecycle = LifecycleState.PIVOT_CONFIRMED
                leg = build_wave_leg(state, pivot, self.config)
                if leg is not None:
                    wave_changed = True
                    state.lifecycle = LifecycleState.WAVE_LEG_CONFIRMED
                build_structure(state, self.config, pivot.confirmed_at)
                state.lifecycle = LifecycleState.STRUCTURE_UPDATED
            if wave_changed:
                build_hypotheses(state, self.config, bar.close_time)
                # A hypothesis created on this completed bar must not be exposed as
                # active when the same bar already breached its invalidation level.
                hypothesis_invalidated |= update_hypothesis_invalidation(state, bar)
                state.lifecycle = LifecycleState.HYPOTHESES_UPDATED
            elif state.structure is None and state.confirmed_pivots:
                build_structure(state, self.config, bar.close_time)
            if not confirmed and state.processed_bars < self.config.warmup_bars:
                state.lifecycle = LifecycleState.WARMING_UP
            elif not confirmed and state.provisional is not None:
                state.lifecycle = LifecycleState.TRACKING_PROVISIONAL_PIVOT
            reasons: set[str] = set()
            if confirmed:
                reasons.add("confirmed")
            if wave_changed:
                reasons.add("wave")
            if hypothesis_invalidated:
                reasons.add("hypothesis")
            if provisional_changed and self.config.publish_provisional:
                reasons.add("provisional")
            if recovering and not recovery_complete:
                # Internal state advances, but no point-in-time feature may escape
                # while the segment is still in mandatory recovery.
                state.lifecycle = LifecycleState.GAP_BLOCKED
                continue
            if recovery_complete:
                state.gap_blocked = False
                reasons.add("recovery_complete")
                if not confirmed and state.provisional is None:
                    state.lifecycle = LifecycleState.WARMING_UP
            if reasons:
                changes[timeframe] = reasons

        if not changes:
            if processed_targets == 0:
                return self._skip(SkipReason.NO_COMPLETED_TARGET_BAR)
            if recovery_targets:
                return self._skip(
                    SkipReason.RECOVERY_WARMUP,
                    recovering_timeframes=recovery_targets,
                    required_bars=self.config.effective_recovery_bars,
                )
            return self._skip(SkipReason.NO_STATE_CHANGE)

        changed_timeframes = tuple(changes)
        publication_digest = self._publication_digest(base, changes)
        features = self._build_features(base, changes, view, publication_digest=publication_digest)
        feature_deletes = self._supersede_persistent_features(features)
        self._state.publications += len(features)
        for timeframe in changed_timeframes:
            self._state.timeframes[timeframe].publications += 1
        diagnostics: Mapping[str, object] = {}
        if self.config.publish_diagnostics:
            diagnostics = {
                "event": "wave_analysis_published",
                "symbol": base.symbol,
                "decision_time": base.close_time.isoformat(),
                "changed_timeframes": changed_timeframes,
                "change_reasons": {key: tuple(sorted(value)) for key, value in changes.items()},
                "state_digest": self._state.digest,
                "publication_digest": publication_digest,
                "health": self._health(),
            }
        return ModuleDecision(features=features, feature_deletes=feature_deletes, diagnostics=diagnostics)

    def snapshot_state(self) -> Mapping[str, object]:
        with self._lock:
            return dict(self._state.to_mapping(self.config))

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise TypeError("wave_analysis state must be a mapping")
        restored = RuntimeState.from_mapping(state, self.config)
        with self._lock:
            self._state = restored

    @property
    def state_digest(self) -> str:
        with self._lock:
            return self._state.digest

    def _prevalidate_targets(self, view: MarketView, base: Candle) -> None:
        """Validate all available target bars before mutating point-in-time state."""
        for timeframe in self.config.timeframes:
            target = base if base.timeframe == timeframe else view.current_for(timeframe)
            if target is None or target.close_time > base.close_time:
                continue
            if target.timeframe != timeframe:
                raise WaveAnalysisDataError(
                    f"target timeframe {target.timeframe!r} does not match configured {timeframe!r}"
                )
            if target.symbol != base.symbol:
                raise WaveAnalysisDataError(
                    f"target symbol {target.symbol!r} does not match base symbol {base.symbol!r}"
                )
            state = self._state.timeframes[timeframe]
            if state.last_open_time is None:
                continue
            fingerprint = candle_fingerprint(target)
            if target.timestamp < state.last_open_time:
                self._state.out_of_order_events += 1
                raise WaveAnalysisDataError(
                    f"out-of-order {timeframe} bar {target.timestamp.isoformat()} "
                    f"after {state.last_open_time.isoformat()}"
                )
            if target.timestamp == state.last_open_time and fingerprint != state.last_fingerprint:
                self._state.conflicting_duplicates += 1
                raise WaveAnalysisDataError(f"conflicting duplicate {timeframe} candle for processed timestamp")

    def _validate_base_event(self, base: Candle, fingerprint: str) -> bool:
        previous = self._state.last_base_open_time
        if previous is None:
            return False
        if base.timestamp < previous:
            self._state.out_of_order_events += 1
            raise WaveAnalysisDataError(
                f"out-of-order base bar {base.timestamp.isoformat()} after {previous.isoformat()}"
            )
        if base.timestamp == previous:
            if fingerprint != self._state.last_base_fingerprint:
                self._state.conflicting_duplicates += 1
                raise WaveAnalysisDataError("conflicting duplicate base candle for the already processed timestamp")
            self._state.duplicate_base_events += 1
            return True
        return False

    def _target_is_duplicate(self, state: TimeframeState, target: Candle, fingerprint: str) -> bool:
        if state.last_open_time is None:
            return False
        if target.timestamp < state.last_open_time:
            self._state.out_of_order_events += 1
            raise WaveAnalysisDataError(
                f"out-of-order {state.timeframe} bar {target.timestamp.isoformat()} after {state.last_open_time.isoformat()}"
            )
        if target.timestamp == state.last_open_time:
            if fingerprint != state.last_fingerprint:
                self._state.conflicting_duplicates += 1
                raise WaveAnalysisDataError(f"conflicting duplicate {state.timeframe} candle for processed timestamp")
            state.duplicate_events += 1
            return True
        return False

    def _bind_symbol(self, base: Candle) -> None:
        if self._state.bound_symbol is None:
            self._state.bound_symbol = base.symbol
            for state in self._state.timeframes.values():
                state.symbol = base.symbol
            return
        if base.symbol != self._state.bound_symbol:
            raise WaveAnalysisDataError(
                f"module instance is bound to {self._state.bound_symbol}, got {base.symbol}; use one instance per instrument"
            )

    def _remember_base(self, base: Candle, fingerprint: str) -> None:
        self._state.last_base_open_time = base.timestamp
        self._state.last_base_fingerprint = fingerprint

    def _mark_gap(self, state: TimeframeState) -> None:
        state.gap_events += 1
        state.gap_blocked = True
        state.recovery_count = 0
        state.segment_id += 1
        state.lifecycle = LifecycleState.GAP_BLOCKED
        state.bar_offset = state.processed_bars
        state.bars.clear()
        state.provisional = None
        state.structure = None
        state.hypotheses.clear()
        state.initial_high = None
        state.initial_low = None
        state.zigzag_direction = None
        state.atr = None
        state.previous_close = None
        state.tr_seed.clear()
        state.atr_values.clear()

    def _publication_digest(self, base: Candle, changes: Mapping[str, set[str]]) -> str:
        tf_payload: dict[str, object] = {}
        for timeframe, reasons in changes.items():
            state = self._state.timeframes[timeframe]
            segment_pivots = current_segment_pivots(state)
            segment_waves = current_segment_waves(state)
            tf_payload[timeframe] = {
                "processed_bars": state.processed_bars,
                "segment_id": state.segment_id,
                "revision_counter": state.revision_counter,
                "lifecycle": state.lifecycle.value,
                "last_pivot_id": None if not segment_pivots else segment_pivots[-1].pivot_id,
                "last_wave_id": None if not segment_waves else segment_waves[-1].wave_id,
                "provisional_revision_id": None if state.provisional is None else state.provisional.revision_id,
                "change_reasons": tuple(sorted(reasons)),
            }
        return stable_id(
            "wa-publication",
            {
                "config": self.config.signature,
                "symbol": base.symbol,
                "decision_time": base.close_time.isoformat(),
                "timeframes": tf_payload,
            },
        )

    def _build_features(
        self,
        base: Candle,
        changes: Mapping[str, set[str]],
        view: MarketView,
        *,
        publication_digest: str,
    ) -> tuple[Feature, ...]:
        features: list[Feature] = []
        regime = self._optional_regime(view, base.symbol)
        for timeframe, reasons in changes.items():
            state = self._state.timeframes[timeframe]
            valid_until = base.close_time + timedelta(seconds=timeframe_seconds(timeframe))
            common = {
                "module_version": MODULE_VERSION,
                "config_fingerprint": self.config.signature,
                "state_schema_version": "1",
                "point_in_time": True,
                "confirmed_non_repainting": True,
                "elliott_probabilistic": True,
                "publication_digest": publication_digest,
                "lifecycle": state.lifecycle.value,
                "optional_market_regime": regime,
            }
            segment_pivots = current_segment_pivots(state)
            segment_waves = current_segment_waves(state)
            latest_wave = segment_waves[-1] if segment_waves else None
            structure_value: object = (
                {
                    "timeframe": timeframe,
                    "state": StructureState.UNDEFINED.value,
                    "as_of": base.close_time.isoformat(),
                    "reason_codes": ["insufficient_confirmed_pivots"],
                }
                if state.structure is None
                else dict(state.structure.to_mapping())
            )
            pivot_snapshot = state.confirmed_pivots[-self.config.feature_snapshot_limit :]
            wave_snapshot = state.wave_legs[-self.config.feature_snapshot_limit :]
            hypotheses = [dict(item.to_mapping()) for item in state.hypotheses]
            primary_hypothesis = next((item for item in hypotheses if item["status"] != "invalidated"), None)
            impulse_score = Decimal("0")
            correction_score = Decimal("0")
            if latest_wave:
                if latest_wave.classification in {WaveClassification.IMPULSE, WaveClassification.EXPANSION}:
                    impulse_score = latest_wave.classification_score
                if latest_wave.classification in {WaveClassification.CORRECTION, WaveClassification.CONTRACTION}:
                    correction_score = latest_wave.classification_score
            values: list[tuple[str, object, datetime | None, bool]] = [
                (
                    CONFIRMED_PIVOTS_FEATURE,
                    {
                        "timeframe": timeframe,
                        "as_of": base.close_time.isoformat(),
                        "items": [dict(item.to_mapping()) for item in pivot_snapshot],
                        "count": len(state.confirmed_pivots),
                        "snapshot_count": len(pivot_snapshot),
                        "snapshot_truncated": len(pivot_snapshot) < len(state.confirmed_pivots),
                        "active_segment_id": state.segment_id,
                        "active_segment_count": len(segment_pivots),
                    },
                    None,
                    True,
                ),
                (
                    WAVE_LEGS_FEATURE,
                    {
                        "timeframe": timeframe,
                        "as_of": base.close_time.isoformat(),
                        "items": [dict(item.to_mapping()) for item in wave_snapshot],
                        "count": len(state.wave_legs),
                        "snapshot_count": len(wave_snapshot),
                        "snapshot_truncated": len(wave_snapshot) < len(state.wave_legs),
                        "active_segment_id": state.segment_id,
                        "active_segment_count": len(segment_waves),
                    },
                    None,
                    True,
                ),
                (
                    CURRENT_WAVE_FEATURE,
                    {
                        "timeframe": timeframe,
                        "as_of": base.close_time.isoformat(),
                        "last_confirmed_wave": None if latest_wave is None else dict(latest_wave.to_mapping()),
                        "provisional_end": None if state.provisional is None else dict(state.provisional.to_mapping()),
                    },
                    valid_until,
                    True,
                ),
                (STRUCTURE_FEATURE, structure_value, valid_until, True),
                (
                    IMPULSE_SCORE_FEATURE,
                    {"timeframe": timeframe, "score": canonical_decimal(impulse_score)},
                    valid_until,
                    True,
                ),
                (
                    CORRECTION_SCORE_FEATURE,
                    {"timeframe": timeframe, "score": canonical_decimal(correction_score)},
                    valid_until,
                    True,
                ),
                (RETRACEMENT_FEATURE, {"timeframe": timeframe, "value": retracement_metrics(state)}, valid_until, True),
                (EXTENSION_FEATURE, {"timeframe": timeframe, "value": extension_metrics(state)}, valid_until, True),
                (
                    INVALIDATION_FEATURE,
                    {
                        "timeframe": timeframe,
                        "price": None
                        if state.structure is None or state.structure.invalidation_price is None
                        else canonical_decimal(state.structure.invalidation_price),
                    },
                    valid_until,
                    True,
                ),
                (
                    HYPOTHESES_FEATURE,
                    {"timeframe": timeframe, "items": hypotheses, "bounded_limit": self.config.maximum_hypotheses},
                    valid_until,
                    True,
                ),
                (PRIMARY_HYPOTHESIS_FEATURE, {"timeframe": timeframe, "item": primary_hypothesis}, valid_until, True),
                (
                    CONFIDENCE_FEATURE,
                    {"timeframe": timeframe, "confidence": canonical_decimal(self._timeframe_confidence(state))},
                    valid_until,
                    True,
                ),
            ]
            if reasons == {"provisional"}:
                values = [values[2]]  # current_wave only; confirmed structure did not change
            if self.config.publish_provisional:
                values.insert(
                    0,
                    (
                        PROVISIONAL_PIVOT_FEATURE,
                        {
                            "timeframe": timeframe,
                            "as_of": base.close_time.isoformat(),
                            "status": "none" if state.provisional is None else "provisional",
                            "item": None if state.provisional is None else dict(state.provisional.to_mapping()),
                        },
                        valid_until,
                        True,
                    ),
                )
            for key, value, lifetime, invalidate_on_gap in values:
                features.append(
                    self._feature(
                        key=key,
                        value=value,
                        base=base,
                        timeframe=timeframe,
                        valid_until=lifetime,
                        invalidate_on_gap=invalidate_on_gap,
                        metadata=common,
                        publication_digest=publication_digest,
                    )
                )

        if any(reasons != {"provisional"} for reasons in changes.values()):
            features.append(
                self._feature(
                    key=MTF_CONTEXT_FEATURE,
                    value=self._mtf_context(base.close_time),
                    base=base,
                    timeframe=self.config.primary_timeframe,
                    valid_until=base.close_time + timedelta(seconds=timeframe_seconds(self.config.primary_timeframe)),
                    invalidate_on_gap=True,
                    metadata={
                        "module_version": MODULE_VERSION,
                        "config_fingerprint": self.config.signature,
                        "point_in_time": True,
                        "optional_market_regime": regime,
                    },
                    publication_digest=publication_digest,
                )
            )
        return tuple(features)

    def _clear_persistent_feature_snapshots(self) -> tuple[FeatureDelete, ...]:
        persistent_keys = {CONFIRMED_PIVOTS_FEATURE, WAVE_LEGS_FEATURE}
        deletions: list[FeatureDelete] = []
        for timeframe, state in sorted(self._state.timeframes.items(), key=lambda item: timeframe_seconds(item[0])):
            for key, feature_id in sorted(state.published_feature_ids.items()):
                if key not in persistent_keys:
                    continue
                deletions.append(
                    FeatureDelete(
                        key=key,
                        source_module=self.name,
                        feature_id=feature_id,
                        symbol=state.symbol,
                        timeframe=timeframe,
                        reason="data_gap_invalidation",
                    )
                )
            state.published_feature_ids.clear()
        return tuple(deletions)

    def _supersede_persistent_features(self, features: tuple[Feature, ...]) -> tuple[FeatureDelete, ...]:
        persistent_keys = {CONFIRMED_PIVOTS_FEATURE, WAVE_LEGS_FEATURE}
        deletions: list[FeatureDelete] = []
        for feature in features:
            if feature.key not in persistent_keys or feature.feature_id is None or feature.timeframe is None:
                continue
            state = self._state.timeframes[feature.timeframe]
            previous = state.published_feature_ids.get(feature.key)
            if previous is not None and previous != feature.feature_id:
                deletions.append(
                    FeatureDelete(
                        key=feature.key,
                        source_module=self.name,
                        feature_id=previous,
                        symbol=feature.symbol,
                        timeframe=feature.timeframe,
                        reason="superseded_snapshot",
                    )
                )
            state.published_feature_ids[feature.key] = feature.feature_id
        return tuple(deletions)

    def _feature(
        self,
        *,
        key: str,
        value: object,
        base: Candle,
        timeframe: str,
        valid_until: datetime | None,
        invalidate_on_gap: bool,
        metadata: Mapping[str, object],
        publication_digest: str,
    ) -> Feature:
        identity = {
            "key": key,
            "symbol": base.symbol,
            "timeframe": timeframe,
            "created_at": base.close_time.isoformat(),
            "publication_digest": publication_digest,
        }
        return Feature(
            key=key,
            source_module=self.name,
            created_at=base.close_time,
            value=value,
            symbol=base.symbol,
            timeframe=timeframe,
            valid_until=valid_until,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=stable_id("wa-feature", identity),
            invalidate_on_gap=invalidate_on_gap,
            metadata=metadata,
        )

    def _mtf_context(self, as_of: datetime) -> Mapping[str, object]:
        ordered = sorted(self.config.timeframes, key=timeframe_seconds)
        primary = self._state.timeframes[self.config.primary_timeframe]
        parent_timeframe = next(
            (tf for tf in ordered if timeframe_seconds(tf) > timeframe_seconds(self.config.primary_timeframe)), None
        )
        parent = None if parent_timeframe is None else self._state.timeframes[parent_timeframe]
        local_state = StructureState.UNDEFINED if primary.structure is None else primary.structure.state
        parent_state = (
            StructureState.UNDEFINED if parent is None or parent.structure is None else parent.structure.state
        )
        directional = {StructureState.BULLISH, StructureState.BEARISH}
        local_waves = current_segment_waves(primary)
        parent_waves = [] if parent is None else current_segment_waves(parent)
        local_wave = local_waves[-1] if local_waves else None
        parent_wave = parent_waves[-1] if parent_waves else None
        compression = expansion = False
        if local_wave is not None and parent_wave is not None and parent_wave.atr_normalized_size > 0:
            ratio = local_wave.atr_normalized_size / parent_wave.atr_normalized_size
            compression = ratio < Decimal("0.618")
            expansion = ratio > Decimal("1.272")
        return {
            "as_of": as_of.isoformat(),
            "local_timeframe": self.config.primary_timeframe,
            "local_wave_direction": None if local_wave is None else local_wave.direction.value,
            "local_structure": local_state.value,
            "parent_timeframe": parent_timeframe,
            "parent_wave_direction": None if parent_wave is None else parent_wave.direction.value,
            "parent_structure": parent_state.value,
            "alignment": local_state == parent_state and local_state in directional,
            "conflict": local_state in directional and parent_state in directional and local_state != parent_state,
            "compression": compression,
            "expansion": expansion,
            "higher_timeframe_invalidation": None
            if parent is None or parent.structure is None or parent.structure.invalidation_price is None
            else canonical_decimal(parent.structure.invalidation_price),
            "timeframes": {
                tf: {
                    "lifecycle": state.lifecycle.value,
                    "structure": StructureState.UNDEFINED.value
                    if state.structure is None
                    else state.structure.state.value,
                    "confirmed_pivots": len(state.confirmed_pivots),
                    "wave_legs": len(state.wave_legs),
                    "active_segment_id": state.segment_id,
                    "active_segment_pivots": len(current_segment_pivots(state)),
                    "active_segment_waves": len(current_segment_waves(state)),
                }
                for tf, state in sorted(self._state.timeframes.items(), key=lambda item: timeframe_seconds(item[0]))
            },
        }

    def _optional_regime(self, view: MarketView, symbol: str) -> object | None:
        if view.features is None:
            return None
        try:
            return cast(object, view.features.get(self.config.optional_market_regime_feature, None, symbol=symbol))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _timeframe_confidence(state: TimeframeState) -> Decimal:
        values: list[Decimal] = []
        if state.structure is not None:
            values.append(state.structure.confidence)
        segment_waves = current_segment_waves(state)
        if segment_waves:
            values.append(segment_waves[-1].confidence)
        if state.hypotheses:
            values.append(state.hypotheses[0].confidence)
        return sum(values, Decimal("0")) / Decimal(len(values)) if values else Decimal("0")

    def _health(self) -> Mapping[str, object]:
        return {
            "processed_base_events": self._state.processed_base_events,
            "duplicate_base_events": self._state.duplicate_base_events,
            "conflicting_duplicates": self._state.conflicting_duplicates,
            "out_of_order_events": self._state.out_of_order_events,
            "gap_events": self._state.gap_events,
            "publications": self._state.publications,
            "timeframes": {
                tf: {
                    "processed_bars": state.processed_bars,
                    "retained_bars": len(state.bars),
                    "confirmed_pivots": len(state.confirmed_pivots),
                    "wave_legs": len(state.wave_legs),
                    "active_segment_id": state.segment_id,
                    "active_segment_pivots": len(current_segment_pivots(state)),
                    "active_segment_waves": len(current_segment_waves(state)),
                    "hypotheses": len(state.hypotheses),
                    "gap_blocked": state.gap_blocked,
                    "recovery_count": state.recovery_count,
                    "revision_counter": state.revision_counter,
                }
                for tf, state in sorted(self._state.timeframes.items())
            },
        }

    def _skip(
        self,
        reason: SkipReason,
        *,
        feature_deletes: tuple[FeatureDelete, ...] = (),
        **details: object,
    ) -> ModuleDecision:
        diagnostics: Mapping[str, object] = {}
        if self.config.publish_diagnostics:
            diagnostics = {
                "event": "wave_analysis_skipped",
                "reason": reason.value,
                "health": self._health(),
                **details,
            }
        return ModuleDecision(
            feature_deletes=feature_deletes,
            skips=(reason.value,),
            diagnostics=diagnostics,
        )
