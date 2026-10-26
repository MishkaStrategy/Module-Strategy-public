from __future__ import annotations

from .module_shared import *  # noqa: F401,F403
from .module_working import _FvgWorkingState


class _FvgOnBarMixin:
    def _early_noop(
        self,
        view: MarketView,
        *,
        current_symbol: str,
        current_close: datetime,
    ) -> ModuleDecision | None:
        if view.data_gap or self._republish_pending or self.config.pre_fvg_enabled:
            return None
        noop_skips: list[str] = []
        duplicate_count = 0
        has_work = False
        for timeframe in self.config.timeframes:
            series = self._series_for(view, timeframe)
            if not series:
                noop_skips.append(self._no_data_reason(timeframe))
                continue
            latest = series[-1]
            latest_close = self._validate_visible_bar(
                latest,
                current_close,
                current_symbol,
                timeframe,
            )
            key = (current_symbol, timeframe)
            previous_close = self._last_processed.get(key)
            if previous_close is not None and latest_close == previous_close:
                expected_signature = self._last_signatures.get(key)
                if expected_signature is not None:
                    if self._raw_signature(latest, latest_close) != expected_signature:
                        raise self._conflicting_duplicate_error(
                            key,
                            previous_close,
                        )
                    self._validate_history_fingerprints(
                        series,
                        key,
                        self._history_fingerprints.get(key, ()),
                        self._history_references.get(key, ()),
                        len(series),
                    )
                    duplicate_count += 1
                else:
                    has_work = True
            else:
                has_work = True
        if has_work:
            return None
        return ModuleDecision(
            skips=tuple(noop_skips),
            diagnostics=self._diagnostics(
                view.index,
                False,
                symbol=current_symbol,
                duplicates=duplicate_count,
            ),
        )

    def _working_state(self, current_symbol: str) -> _FvgWorkingState:
        return _FvgWorkingState(
            zones=dict(self._zones),
            lifecycle=dict(self._zone_lifecycle),
            candidates=dict(self._pre_candidates),
            pre_evaluated=dict(self._pre_evaluated),
            last=dict(self._last_processed),
            fingerprints=dict(self._last_fingerprints),
            history_fingerprints=dict(self._history_fingerprints),
            history_references=dict(self._history_references),
            signatures=dict(self._last_signatures),
            barriers=dict(self._barriers),
            bound_symbol=self._bound_symbol or current_symbol,
            republish_pending=self._republish_pending,
        )

    def _reset_gap(
        self,
        *,
        symbol: str,
        state: _FvgWorkingState,
        deletes: list[FeatureDelete],
        skips: list[str],
        diagnostics: dict[str, object],
    ) -> None:
        cleared = self._clear_symbol(
            symbol,
            state.zones,
            state.last,
            state.fingerprints,
            state.history_fingerprints,
            state.history_references,
            state.signatures,
            state.barriers,
        )
        for zone in cleared:
            state.lifecycle.pop(zone.zone_id, None)
            deletes.append(self._delete(zone, FvgReasonCode.DATA_GAP_DELETE))
        removed_candidates = tuple(candidate for candidate in state.candidates.values() if candidate.symbol == symbol)
        for candidate in removed_candidates:
            state.candidates.pop(candidate.candidate_id, None)
            deletes.append(
                self._pre_candidate_delete(
                    candidate,
                    FvgReasonCode.PRE_CANDIDATE_GAP_DELETE,
                )
            )
        for key in tuple(state.pre_evaluated):
            if key[0] == symbol:
                state.pre_evaluated.pop(key, None)
        diagnostics["gap_cleared_zones"] = len(cleared)
        diagnostics["gap_cleared_pre_candidates"] = len(removed_candidates)
        skips.append(FvgReasonCode.DATA_GAP_RESET.value)
        state.republish_pending = False

    def _restored_persistent_features_are_visible(
        self,
        view: MarketView,
        state: _FvgWorkingState,
    ) -> bool:
        feature_view = view.features
        if feature_view is None:
            return False
        expected: set[tuple[str, str]] = {(ACTIVE_ZONE_KEY, zone.zone_id) for zone in state.zones.values()}
        expected.update((PRE_CANDIDATE_KEY, candidate.candidate_id) for candidate in state.candidates.values())
        if not expected:
            return False
        visible: set[tuple[str, str]] = set()
        for key in (ACTIVE_ZONE_KEY, PRE_CANDIDATE_KEY):
            for feature in feature_view.get_all(key, source_module=self.name):
                if feature.feature_id is not None:
                    visible.add((key, feature.feature_id))
        return expected.issubset(visible)

    def _republish_restored(
        self,
        *,
        current_close: datetime,
        state: _FvgWorkingState,
        features: list[Feature],
        diagnostics: dict[str, object],
    ) -> None:
        for zone in sorted(
            state.zones.values(),
            key=lambda item: item.zone_id,
        ):
            features.append(self._active_feature(zone))
            lifecycle = state.lifecycle.get(zone.zone_id)
            if lifecycle is not None:
                features.append(
                    self._zone_state_feature(
                        zone,
                        lifecycle,
                        current_close,
                    )
                )
        for candidate in sorted(
            state.candidates.values(),
            key=lambda item: item.candidate_id,
        ):
            features.append(self._restored_pre_candidate_feature(candidate))
        diagnostics["republished_after_restore"] = len(state.zones)
        diagnostics["pre_republished_after_restore"] = len(state.candidates)

    def _commit_working_state(
        self,
        state: _FvgWorkingState,
        candidate_size: int,
    ) -> None:
        self._zones = state.zones
        self._zone_lifecycle = state.lifecycle
        self._pre_candidates = state.candidates
        self._pre_evaluated = state.pre_evaluated
        self._last_processed = state.last
        self._last_fingerprints = state.fingerprints
        self._history_fingerprints = state.history_fingerprints
        self._history_references = state.history_references
        self._last_signatures = state.signatures
        self._barriers = state.barriers
        self._bound_symbol = state.bound_symbol
        self._republish_pending = False
        self._state_size_bytes = candidate_size

    def on_bar(self, view: MarketView) -> ModuleDecision:
        if not isinstance(view, MarketView):
            raise TypeError("view must be a MarketView")
        if not view.base_bars:
            return ModuleDecision(
                skips=(FvgReasonCode.EMPTY_BASE_BARS.value,),
                diagnostics=self._diagnostics(
                    view.index,
                    view.data_gap,
                ),
            )

        current = view.current
        _, current_close = _validate_candle_clock(current, "current")
        current_symbol = _required_candle_attribute(
            current,
            "current",
            "symbol",
        )
        if type(current_symbol) is not str:
            raise TypeError("current candle symbol must be a canonical string")
        if self._bound_symbol is not None and current_symbol != self._bound_symbol:
            raise FvgSymbolScopeError(
                f"FVG module instance is bound to {self._bound_symbol}, received {current_symbol}"
            )

        early = self._early_noop(
            view,
            current_symbol=current_symbol,
            current_close=current_close,
        )
        if early is not None:
            return early

        state = self._working_state(current_symbol)
        features: list[Feature] = []
        deletes: list[FeatureDelete] = []
        skips: list[str] = []
        diagnostics: dict[str, object] = dict(
            self._diagnostics(
                view.index,
                view.data_gap,
                symbol=current_symbol,
            )
        )

        if view.data_gap:
            self._reset_gap(
                symbol=current_symbol,
                state=state,
                deletes=deletes,
                skips=skips,
                diagnostics=diagnostics,
            )
        elif state.republish_pending:
            if self._restored_persistent_features_are_visible(view, state):
                diagnostics["restore_republish_suppressed"] = len(state.zones)
                diagnostics["pre_restore_republish_suppressed"] = len(state.candidates)
                state.republish_pending = False
            else:
                self._republish_restored(
                    current_close=current_close,
                    state=state,
                    features=features,
                    diagnostics=diagnostics,
                )

        for timeframe in self.config.timeframes:
            self._process_timeframe(
                view=view,
                timeframe=timeframe,
                current_symbol=current_symbol,
                visible_until=current_close,
                gap_reset=view.data_gap,
                state=state,
                features=features,
                deletes=deletes,
                skips=skips,
                diagnostics=diagnostics,
            )

        candidate_state = self._state_payload_from(
            zones=state.zones,
            zone_lifecycle=state.lifecycle,
            pre_candidates=state.candidates,
            pre_evaluated=state.pre_evaluated,
            last_processed=state.last,
            fingerprints=state.fingerprints,
            history_fingerprints=state.history_fingerprints,
            barriers=state.barriers,
            bound_symbol=state.bound_symbol,
        )
        candidate_size = self._state_size(candidate_state)
        if candidate_size > self.config.max_state_bytes:
            raise FvgStateError(
                f"FVG state is {candidate_size} bytes, exceeds configured limit {self.config.max_state_bytes}"
            )

        self._commit_working_state(state, candidate_size)
        diagnostics["active_total"] = len(self._zones)
        diagnostics["active_by_timeframe"] = dict(sorted(self._active_counts().items()))
        diagnostics["pre_candidate_total"] = len(self._pre_candidates)
        diagnostics["state_estimated_bytes"] = candidate_size
        return ModuleDecision(
            features=tuple(features),
            feature_deletes=tuple(deletes),
            skips=tuple(skips),
            diagnostics=MappingProxyType(diagnostics),
        )
