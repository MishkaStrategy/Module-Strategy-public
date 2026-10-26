from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgRuntimeMixin:
    @staticmethod
    def _canonical_close_time(bar: CandleLike, label: str) -> datetime:
        raw_close = _required_candle_attribute(bar, label, "close_time")
        if not isinstance(raw_close, datetime):
            raise TypeError(f"{label} candle close_time must be a datetime")
        return as_utc(raw_close, f"{label}.close_time")

    @staticmethod
    def _validate_visible_bar(
        bar: CandleLike,
        visible_until: datetime,
        symbol: str,
        timeframe: str,
    ) -> datetime:
        label = f"visible {timeframe}"
        bar_symbol = _required_candle_attribute(bar, label, "symbol")
        bar_timeframe = _required_candle_attribute(bar, label, "timeframe")
        if type(bar_symbol) is not str or type(bar_timeframe) is not str:
            raise TypeError("visible candle symbol and timeframe must be canonical strings")
        if bar_symbol != symbol:
            raise ValueError("timeframe series symbol differs from current symbol")
        if bar_timeframe != timeframe:
            raise ValueError("timeframe series contains a candle with an unexpected timeframe")
        _, canonical_close = _validate_candle_clock(bar, label)
        canonical_visible_until = as_utc(visible_until, "visible_until")
        if canonical_close > canonical_visible_until:
            raise ValueError(f"future/incomplete {timeframe} candle exposed to FVG module")
        return canonical_close

    @classmethod
    def _new_indexes_canonical(
        cls,
        series: Sequence[CandleLike],
        previous_close: datetime | None,
    ) -> range:
        if previous_close is None:
            return range(len(series))
        start = len(series) - 1
        while start >= 0:
            close_time = cls._canonical_close_time(series[start], f"series[{start}]")
            if close_time <= previous_close:
                break
            start -= 1
        return range(start + 1, len(series))

    @staticmethod
    def _validate_history_fingerprints(
        series: Sequence[CandleLike],
        key: tuple[str, str],
        history: tuple[tuple[datetime, str], ...],
        reference_history: tuple[_HistoryReference, ...],
        boundary_index: int,
    ) -> tuple[_HistoryReference, ...]:
        if not history:
            return ()
        if boundary_index < len(history):
            missing = history[0][0].isoformat()
            raise FvgDataConflictError(f"historical candle missing for {key[0]} {key[1]} at {missing}")
        candidates = series[boundary_index - len(history) : boundary_index]
        references = dict(reference_history)
        validated: list[_HistoryReference] = []
        duplicate_replay = boundary_index == len(series)
        latest_history_index = len(history) - 1
        for history_index, ((expected_close, expected_fingerprint), bar) in enumerate(
            zip(history, candidates, strict=True)
        ):
            known_bar = references.get(expected_close)
            if not (isinstance(bar, Candle) and known_bar is bar):
                try:
                    observed = candle_fingerprint(bar)
                    observed_close = _FvgRuntimeMixin._canonical_close_time(bar, f"history[{history_index}]")
                except (TypeError, ValueError, AttributeError) as exc:
                    if duplicate_replay and history_index == latest_history_index:
                        raise FvgDataConflictError(
                            f"conflicting duplicate for {key[0]} {key[1]} at {expected_close.isoformat()}"
                        ) from exc
                    raise FvgDataConflictError(
                        f"historical candle changed for {key[0]} {key[1]} at {expected_close.isoformat()}"
                    ) from exc
                if observed_close != expected_close or observed != expected_fingerprint:
                    if duplicate_replay and history_index == latest_history_index:
                        raise FvgDataConflictError(
                            f"conflicting duplicate for {key[0]} {key[1]} at {expected_close.isoformat()}"
                        )
                    raise FvgDataConflictError(
                        f"historical candle changed for {key[0]} {key[1]} at {expected_close.isoformat()}"
                    )
            validated.append((expected_close, bar))
        return tuple(validated)

    @staticmethod
    def _increment_reason(
        diagnostics: dict[str, object],
        reason: str,
    ) -> None:
        raw = diagnostics.get("evaluation_reasons")
        if not isinstance(raw, dict):
            raise TypeError("evaluation_reasons diagnostic must be a mapping")
        value = raw.get(reason, 0)
        if type(value) is not int:
            raise TypeError("evaluation reason counter must be an integer")
        raw[reason] = value + 1

    def _evaluate_confirmed_window(
        self,
        *,
        series: Sequence[CandleLike],
        index: int,
        effective_config: FvgConfig,
        visible_until: datetime,
    ) -> tuple[FvgEvaluation | None, FvgZone | None]:
        a, b, c = series[index - 2], series[index - 1], series[index]
        if not effective_config.requires_confirmed_evaluation:
            return None, _detect_zone_validated(a, b, c, effective_config)
        evaluation = evaluate_fvg_window(
            a=a,
            b=b,
            c=c,
            previous_bars=tuple(series[: index - 2]),
            config=effective_config,
            available_at=visible_until,
        )
        return evaluation, evaluation.zone

    def _observe_zones_for_bar(
        self,
        *,
        timeframe: str,
        bar: CandleLike,
        bar_close: datetime,
        visible_until: datetime,
        zones: dict[str, FvgZone],
        lifecycle: dict[str, FvgZoneLifecycle],
        features: list[Feature],
        deletes: list[FeatureDelete],
        diagnostics: dict[str, object],
    ) -> None:
        duration_seconds = timeframe_seconds(timeframe)
        bar_low = as_decimal(_required_candle_attribute(bar, "lifecycle bar", "low"))
        bar_high = as_decimal(_required_candle_attribute(bar, "lifecycle bar", "high"))
        bar_close_price = as_decimal(_required_candle_attribute(bar, "lifecycle bar", "close"))
        symbol = _required_candle_attribute(bar, "lifecycle bar", "symbol")
        for zone_id in sorted(tuple(zones)):
            zone = zones.get(zone_id)
            if zone is None or zone.symbol != symbol or zone.timeframe != timeframe or zone.created_at >= bar_close:
                continue
            current_lifecycle = lifecycle.get(zone_id)
            if current_lifecycle is None:
                current_lifecycle = initial_lifecycle(zone)
            observed = _observe_lifecycle_validated(
                zone,
                current_lifecycle,
                bar_close=bar_close,
                duration_seconds=duration_seconds,
                bar_low=bar_low,
                bar_high=bar_high,
            )
            lifecycle[zone_id] = observed

            invalidated = self._is_invalidated_values(
                zone,
                bar_low=bar_low,
                bar_high=bar_high,
                bar_close=bar_close_price,
            )
            expired = (
                not invalidated
                and self.config.max_zone_age_bars > 0
                and observed.age_bars >= self.config.max_zone_age_bars
            )
            if invalidated:
                terminal_status = (
                    FvgLifecycleStatus.FULLY_FILLED
                    if observed.maximum_penetration >= Decimal("1")
                    else FvgLifecycleStatus.INVALIDATED
                )
                terminal = terminal_lifecycle(observed, terminal_status, bar_close)
                features.append(self._zone_state_feature(zone, terminal, visible_until))
                zones.pop(zone_id, None)
                lifecycle.pop(zone_id, None)
                deletes.append(self._delete(zone, FvgReasonCode.MITIGATED_DELETE))
                if self.config.publish_lifecycle_events:
                    features.append(
                        self._lifecycle_feature(
                            INVALIDATED_EVENT_KEY,
                            zone,
                            visible_until,
                            bar,
                        )
                    )
                self._increment_counter(diagnostics, "invalidated")
                continue
            if expired:
                terminal = terminal_lifecycle(observed, FvgLifecycleStatus.EXPIRED, bar_close)
                features.append(self._zone_state_feature(zone, terminal, visible_until))
                zones.pop(zone_id, None)
                lifecycle.pop(zone_id, None)
                deletes.append(self._delete(zone, FvgReasonCode.EXPIRED_DELETE))
                self._increment_counter(diagnostics, "expired")
                continue
            features.append(self._zone_state_feature(zone, observed, visible_until))

    def _evaluate_pre_for_timeframe(
        self,
        *,
        view: MarketView,
        target_series: Sequence[CandleLike],
        timeframe: str,
        visible_until: datetime,
        effective_config: FvgConfig,
        candidates: dict[str, FvgPreCandidate],
        evaluated: dict[tuple[str, str], datetime],
        features: list[Feature],
        diagnostics: dict[str, object],
    ) -> None:
        if not effective_config.pre_fvg_enabled or len(target_series) < 2:
            return
        if timeframe == view.current.timeframe:
            return
        a, b = target_series[-2], target_series[-1]
        b_close = self._canonical_close_time(b, "pre-FVG source B")
        source_c_open = b_close
        source_c_close = period_close(source_c_open, timeframe)
        if visible_until >= source_c_close:
            return
        symbol = _required_candle_attribute(b, "pre-FVG source B", "symbol")
        key = (symbol, timeframe)
        if evaluated.get(key) == source_c_open:
            return
        result = evaluate_pre_fvg(
            a=a,
            b=b,
            lower_bars=cast(Sequence[CandleLike], view.base_bars),
            observed_at=visible_until,
            config=effective_config,
            previous_target_bars=tuple(target_series[:-2]),
        )
        if result.status is FvgPreStatus.NOT_DUE:
            return

        evaluated[key] = source_c_open
        features.append(self._pre_evaluation_feature(result, a, b, visible_until))
        if result.status is FvgPreStatus.CANDIDATE:
            if result.candidate is None:
                raise FvgStateError("candidate pre-FVG evaluation is missing its candidate")
            candidates[result.candidate.candidate_id] = result.candidate
            self._increment_counter(diagnostics, "pre_candidates")
        elif result.status is FvgPreStatus.REJECTED:
            self._increment_counter(diagnostics, "pre_rejected")
        else:
            self._increment_counter(diagnostics, "pre_unavailable_with_current_data")
        self._increment_reason(diagnostics, result.reason)
