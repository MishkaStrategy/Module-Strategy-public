from __future__ import annotations

from .module_shared import *  # noqa: F401,F403
from .module_working import _FvgWorkingState


class _FvgProcessingMixin:
    def _remove_order_scope(
        self,
        *,
        symbol: str,
        timeframe: str,
        key: tuple[str, str],
        latest_close: datetime,
        previous_close: datetime,
        state: _FvgWorkingState,
        deletes: list[FeatureDelete],
        skips: list[str],
    ) -> None:
        removed = self._handle_order_violation(
            key=key,
            latest_close=latest_close,
            previous_close=previous_close,
            zones=state.zones,
            last_processed=state.last,
            fingerprints=state.fingerprints,
            history_fingerprints=state.history_fingerprints,
            history_references=state.history_references,
            signatures=state.signatures,
            barriers=state.barriers,
            skips=skips,
        )
        for zone in removed:
            state.lifecycle.pop(zone.zone_id, None)
            deletes.append(
                self._delete(
                    zone,
                    FvgReasonCode.ORDER_VIOLATION_DELETE,
                )
            )
        removed_pre = self._clear_pre_timeframe_scope(
            symbol,
            timeframe,
            state.candidates,
            state.pre_evaluated,
        )
        for candidate in removed_pre:
            deletes.append(
                self._pre_candidate_delete(
                    candidate,
                    FvgReasonCode.PRE_CANDIDATE_ORDER_DELETE,
                )
            )

    def _publish_confirmed_window(
        self,
        *,
        series: Sequence[CandleLike],
        index: int,
        timeframe: str,
        key: tuple[str, str],
        bar: CandleLike,
        visible_until: datetime,
        effective_config: FvgConfig,
        state: _FvgWorkingState,
        features: list[Feature],
        deletes: list[FeatureDelete],
        diagnostics: dict[str, object],
    ) -> None:
        if index < 2:
            return
        a = series[index - 2]
        barrier = state.barriers.get(key)
        a_close = self._canonical_close_time(a, "FVG source A")
        if barrier is not None and a_close <= barrier:
            return

        evaluation, detected_zone = self._evaluate_confirmed_window(
            series=series,
            index=index,
            effective_config=effective_config,
            visible_until=visible_until,
        )
        if evaluation is not None:
            if effective_config.publish_evaluation_events:
                features.append(
                    self._evaluation_feature(
                        evaluation,
                        bar,
                        visible_until,
                    )
                )
            if evaluation.status is FvgEvaluationStatus.ACCEPTED:
                self._increment_counter(
                    diagnostics,
                    "evaluations_accepted",
                )
            elif evaluation.status is FvgEvaluationStatus.REJECTED:
                self._increment_counter(
                    diagnostics,
                    "evaluations_rejected",
                )
            else:
                self._increment_counter(
                    diagnostics,
                    "evaluations_unavailable_with_current_data",
                )
            self._increment_reason(diagnostics, evaluation.reason)

        conversion_features, resolved_ids = self._pre_conversion_features(
            evaluation=evaluation,
            trigger_bar=bar,
            visible_until=visible_until,
            candidates=state.candidates,
        )
        features.extend(conversion_features)
        for candidate_id in resolved_ids:
            candidate = state.candidates.pop(candidate_id, None)
            if candidate is not None:
                deletes.append(
                    self._pre_candidate_delete(
                        candidate,
                        FvgReasonCode.PRE_CANDIDATE_RESOLVED_DELETE,
                    )
                )
        self._increment_counter(
            diagnostics,
            "pre_resolved",
            len(resolved_ids),
        )

        if detected_zone is None or detected_zone.zone_id in state.zones:
            return
        state.zones[detected_zone.zone_id] = detected_zone
        zone_lifecycle = initial_lifecycle(detected_zone)
        state.lifecycle[detected_zone.zone_id] = zone_lifecycle
        features.append(self._active_feature(detected_zone))
        features.append(
            self._zone_state_feature(
                detected_zone,
                zone_lifecycle,
                visible_until,
            )
        )
        if self.config.publish_lifecycle_events:
            features.append(
                self._lifecycle_feature(
                    CREATED_EVENT_KEY,
                    detected_zone,
                    visible_until,
                    bar,
                )
            )
        self._increment_counter(diagnostics, "created")

        evicted = self._enforce_capacity(
            state.zones,
            detected_zone.symbol,
            timeframe,
        )
        for zone in evicted:
            evicted_lifecycle = state.lifecycle.pop(
                zone.zone_id,
                None,
            )
            if evicted_lifecycle is not None:
                terminal = terminal_lifecycle(
                    evicted_lifecycle,
                    FvgLifecycleStatus.ARCHIVED,
                    evicted_lifecycle.last_observed_at,
                )
                features.append(
                    self._zone_state_feature(
                        zone,
                        terminal,
                        visible_until,
                    )
                )
            deletes.append(
                self._delete(
                    zone,
                    FvgReasonCode.CAPACITY_EVICTED_DELETE,
                )
            )
        self._increment_counter(
            diagnostics,
            "capacity_evicted",
            len(evicted),
        )

    def _process_new_bar(
        self,
        *,
        series: Sequence[CandleLike],
        index: int,
        timeframe: str,
        key: tuple[str, str],
        current_symbol: str,
        visible_until: datetime,
        effective_config: FvgConfig,
        state: _FvgWorkingState,
        features: list[Feature],
        deletes: list[FeatureDelete],
        skips: list[str],
        diagnostics: dict[str, object],
    ) -> bool:
        bar = series[index]
        bar_close = self._validate_visible_bar(
            bar,
            visible_until,
            current_symbol,
            timeframe,
        )
        fingerprint = candle_fingerprint(bar, bar_close)
        signature = self._raw_signature(bar, bar_close)
        last_seen = state.last.get(key)

        if last_seen is not None and bar_close == last_seen:
            expected_signature = state.signatures.get(key)
            if expected_signature is not None:
                if signature != expected_signature:
                    raise self._conflicting_duplicate_error(
                        key,
                        last_seen,
                    )
            else:
                expected = state.fingerprints.get(key)
                if expected is not None and fingerprint != expected:
                    raise self._conflicting_duplicate_error(
                        key,
                        last_seen,
                    )
                state.signatures[key] = signature
            state.fingerprints[key] = fingerprint
            state.history_fingerprints[key] = self._append_history_fingerprint(
                state.history_fingerprints.get(key, ()),
                bar_close,
                fingerprint,
            )
            state.history_references[key] = self._append_history_reference(
                state.history_references.get(key, ()),
                bar_close,
                bar,
            )
            self._increment_counter(diagnostics, "duplicates")
            return True

        if last_seen is not None and bar_close < last_seen:
            self._remove_order_scope(
                symbol=current_symbol,
                timeframe=timeframe,
                key=key,
                latest_close=bar_close,
                previous_close=last_seen,
                state=state,
                deletes=deletes,
                skips=skips,
            )
            self._increment_counter(
                diagnostics,
                "order_violation_resets",
            )
            return False

        self._observe_zones_for_bar(
            timeframe=timeframe,
            bar=bar,
            bar_close=bar_close,
            visible_until=visible_until,
            zones=state.zones,
            lifecycle=state.lifecycle,
            features=features,
            deletes=deletes,
            diagnostics=diagnostics,
        )
        self._publish_confirmed_window(
            series=series,
            index=index,
            timeframe=timeframe,
            key=key,
            bar=bar,
            visible_until=visible_until,
            effective_config=effective_config,
            state=state,
            features=features,
            deletes=deletes,
            diagnostics=diagnostics,
        )

        state.last[key] = bar_close
        state.fingerprints[key] = fingerprint
        state.history_fingerprints[key] = self._append_history_fingerprint(
            state.history_fingerprints.get(key, ()),
            bar_close,
            fingerprint,
        )
        state.history_references[key] = self._append_history_reference(
            state.history_references.get(key, ()),
            bar_close,
            bar,
        )
        state.signatures[key] = signature
        self._increment_counter(diagnostics, "processed_bars")
        return True

    def _process_timeframe(
        self,
        *,
        view: MarketView,
        timeframe: str,
        current_symbol: str,
        visible_until: datetime,
        gap_reset: bool,
        state: _FvgWorkingState,
        features: list[Feature],
        deletes: list[FeatureDelete],
        skips: list[str],
        diagnostics: dict[str, object],
    ) -> None:
        effective_config = self.config.for_timeframe(timeframe)
        series = self._series_for(view, timeframe)
        key = (current_symbol, timeframe)
        if not series:
            skips.append(self._no_data_reason(timeframe))
            if gap_reset:
                state.last[key] = visible_until
                state.fingerprints[key] = None
                state.history_fingerprints[key] = ()
                state.history_references[key] = ()
                state.signatures[key] = None
                state.barriers[key] = visible_until
            return

        latest = series[-1]
        latest_close = self._validate_visible_bar(
            latest,
            visible_until,
            current_symbol,
            timeframe,
        )
        if gap_reset:
            latest_fingerprint = candle_fingerprint(
                latest,
                latest_close,
            )
            canonical_latest_close = as_utc(
                latest_close,
                "gap_reset.close_time",
            )
            state.last[key] = canonical_latest_close
            state.fingerprints[key] = latest_fingerprint
            state.history_fingerprints[key] = ((canonical_latest_close, latest_fingerprint),)
            state.history_references[key] = ((canonical_latest_close, latest),)
            state.signatures[key] = self._raw_signature(
                latest,
                latest_close,
            )
            state.barriers[key] = visible_until
            return

        previous_close = state.last.get(key)
        if previous_close is not None and latest_close < previous_close:
            self._remove_order_scope(
                symbol=current_symbol,
                timeframe=timeframe,
                key=key,
                latest_close=latest_close,
                previous_close=previous_close,
                state=state,
                deletes=deletes,
                skips=skips,
            )
            return

        if previous_close is not None and latest_close == previous_close:
            state.history_references[key] = self._validate_history_fingerprints(
                series,
                key,
                state.history_fingerprints.get(key, ()),
                state.history_references.get(key, ()),
                len(series),
            )
            signature = self._raw_signature(latest, latest_close)
            expected_signature = state.signatures.get(key)
            if expected_signature is not None:
                if signature != expected_signature:
                    raise self._conflicting_duplicate_error(
                        key,
                        previous_close,
                    )
            else:
                observed = candle_fingerprint(
                    latest,
                    latest_close,
                )
                expected = state.fingerprints.get(key)
                if expected is not None and observed != expected:
                    raise self._conflicting_duplicate_error(
                        key,
                        previous_close,
                    )
                state.fingerprints[key] = observed
                state.history_fingerprints[key] = self._append_history_fingerprint(
                    state.history_fingerprints.get(key, ()),
                    latest_close,
                    observed,
                )
                state.history_references[key] = self._append_history_reference(
                    state.history_references.get(key, ()),
                    latest_close,
                    latest,
                )
                state.signatures[key] = signature
            self._increment_counter(diagnostics, "duplicates")
        else:
            new_indexes = self._new_indexes_canonical(
                series,
                previous_close,
            )
            if previous_close is not None:
                state.history_references[key] = self._validate_history_fingerprints(
                    series,
                    key,
                    state.history_fingerprints.get(key, ()),
                    state.history_references.get(key, ()),
                    new_indexes.start,
                )
            if not new_indexes:
                self._increment_counter(diagnostics, "duplicates")
            for index in new_indexes:
                if not self._process_new_bar(
                    series=series,
                    index=index,
                    timeframe=timeframe,
                    key=key,
                    current_symbol=current_symbol,
                    visible_until=visible_until,
                    effective_config=effective_config,
                    state=state,
                    features=features,
                    deletes=deletes,
                    skips=skips,
                    diagnostics=diagnostics,
                ):
                    break

        self._evaluate_pre_for_timeframe(
            view=view,
            target_series=series,
            timeframe=timeframe,
            visible_until=visible_until,
            effective_config=effective_config,
            candidates=state.candidates,
            evaluated=state.pre_evaluated,
            features=features,
            diagnostics=diagnostics,
        )
