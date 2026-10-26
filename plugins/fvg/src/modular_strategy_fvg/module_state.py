from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgStateMixin:
    def _state_payload(self) -> Mapping[str, object]:
        return self._state_payload_from(
            zones=self._zones,
            zone_lifecycle=self._zone_lifecycle,
            pre_candidates=self._pre_candidates,
            pre_evaluated=self._pre_evaluated,
            last_processed=self._last_processed,
            fingerprints=self._last_fingerprints,
            history_fingerprints=self._history_fingerprints,
            barriers=self._barriers,
            bound_symbol=self._bound_symbol,
        )

    def _state_payload_from(
        self,
        *,
        zones: Mapping[str, FvgZone],
        zone_lifecycle: Mapping[str, FvgZoneLifecycle],
        pre_candidates: Mapping[str, FvgPreCandidate],
        pre_evaluated: Mapping[tuple[str, str], datetime],
        last_processed: Mapping[tuple[str, str], datetime],
        fingerprints: Mapping[tuple[str, str], str | None],
        history_fingerprints: Mapping[tuple[str, str], tuple[tuple[datetime, str], ...]],
        barriers: Mapping[tuple[str, str], datetime],
        bound_symbol: str | None,
    ) -> Mapping[str, object]:
        return {
            "state_schema_version": STATE_SCHEMA_VERSION,
            "module_version": MODULE_VERSION,
            "config_fingerprint": self._config_fingerprint,
            "bound_symbol": bound_symbol,
            "zone_lifecycle": [dict(zone_lifecycle[zone_id].to_state()) for zone_id in sorted(zone_lifecycle)],
            "pre_evaluated": [
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "source_c_open_time": source_c_open_time.isoformat(),
                }
                for (symbol, timeframe), source_c_open_time in sorted(pre_evaluated.items())
            ],
            "pre_candidates": [
                dict(pre_candidates[candidate_id].to_state()) for candidate_id in sorted(pre_candidates)
            ],
            "last_processed": [
                {
                    "symbol": symbol,
                    "timeframe": timeframe,
                    "close_time": close_time.isoformat(),
                    "bar_fingerprint": fingerprints.get((symbol, timeframe)),
                    "history_fingerprints": [
                        {
                            "close_time": history_close.isoformat(),
                            "bar_fingerprint": history_fingerprint,
                        }
                        for history_close, history_fingerprint in history_fingerprints.get((symbol, timeframe), ())
                    ],
                    "barrier_close_time": (
                        barriers[(symbol, timeframe)].isoformat() if (symbol, timeframe) in barriers else None
                    ),
                }
                for (symbol, timeframe), close_time in sorted(last_processed.items())
            ],
            "zones": [dict(zones[zone_id].to_state()) for zone_id in sorted(zones)],
        }

    @staticmethod
    def _zone_state_row_size_fast(zone: FvgZone) -> int:
        return _ZONE_ROW_OVERHEAD + sum(
            (
                _json_string_size(zone.zone_id),
                _json_string_size(zone.symbol),
                _json_string_size(zone.timeframe),
                _json_string_size(zone.direction.value),
                _json_decimal_string_size(zone.lower),
                _json_decimal_string_size(zone.upper),
                _iso_datetime_string_size(zone.created_at),
                _iso_datetime_string_size(zone.source_a_open_time),
                _iso_datetime_string_size(zone.source_b_open_time),
                _iso_datetime_string_size(zone.source_c_open_time),
            )
        )

    @staticmethod
    def _lifecycle_state_row_size_fast(lifecycle: FvgZoneLifecycle) -> int:
        return _LIFECYCLE_ROW_OVERHEAD + sum(
            (
                _json_string_size(lifecycle.zone_id),
                _json_string_size(lifecycle.status.value),
                _iso_datetime_string_size(lifecycle.detected_at),
                _iso_datetime_string_size(lifecycle.last_transition_at),
                _iso_datetime_string_size(lifecycle.last_observed_at),
                4 if lifecycle.first_touch_at is None else _iso_datetime_string_size(lifecycle.first_touch_at),
                4 if lifecycle.last_touch_at is None else _iso_datetime_string_size(lifecycle.last_touch_at),
                _json_int_size(lifecycle.contact_count),
                _json_decimal_string_size(lifecycle.fill_percentage),
                _json_decimal_string_size(lifecycle.maximum_penetration),
                _json_int_size(lifecycle.age_bars),
            )
        )

    @staticmethod
    def _history_state_row_size_fast(close_time: datetime, fingerprint: str) -> int:
        return _HISTORY_ROW_OVERHEAD + _iso_datetime_string_size(close_time) + _json_string_size(fingerprint)

    @classmethod
    def _cursor_state_row_size_fast(
        cls,
        *,
        symbol: str,
        timeframe: str,
        close_time: datetime,
        fingerprint: str | None,
        history: tuple[tuple[datetime, str], ...],
        barrier: datetime | None,
    ) -> int:
        history_size = _json_array_size(
            [cls._history_state_row_size_fast(item_close, item_fingerprint) for item_close, item_fingerprint in history]
        )
        return _CURSOR_ROW_OVERHEAD + sum(
            (
                _json_string_size(symbol),
                _json_string_size(timeframe),
                _iso_datetime_string_size(close_time),
                4 if fingerprint is None else _json_string_size(fingerprint),
                history_size,
                4 if barrier is None else _iso_datetime_string_size(barrier),
            )
        )

    def _state_size_fast(
        self,
        *,
        zones: Mapping[str, FvgZone],
        zone_lifecycle: Mapping[str, FvgZoneLifecycle],
        pre_candidates: Mapping[str, FvgPreCandidate],
        pre_evaluated: Mapping[tuple[str, str], datetime],
        last_processed: Mapping[tuple[str, str], datetime],
        fingerprints: Mapping[tuple[str, str], str | None],
        history_fingerprints: Mapping[tuple[str, str], tuple[tuple[datetime, str], ...]],
        barriers: Mapping[tuple[str, str], datetime],
        bound_symbol: str | None,
    ) -> int:
        zone_size = _json_array_size([self._zone_state_row_size_fast(zone) for zone in zones.values()])
        lifecycle_size = _json_array_size(
            [self._lifecycle_state_row_size_fast(lifecycle) for lifecycle in zone_lifecycle.values()]
        )
        pre_candidate_size = _json_array_size(
            [self._state_size(dict(candidate.to_state())) for candidate in pre_candidates.values()]
        )
        pre_evaluated_size = _json_array_size(
            [
                _PRE_EVALUATED_ROW_OVERHEAD
                + _json_string_size(symbol)
                + _json_string_size(timeframe)
                + _iso_datetime_string_size(source_c_open_time)
                for (symbol, timeframe), source_c_open_time in pre_evaluated.items()
            ]
        )
        cursor_size = _json_array_size(
            [
                self._cursor_state_row_size_fast(
                    symbol=symbol,
                    timeframe=timeframe,
                    close_time=close_time,
                    fingerprint=fingerprints.get((symbol, timeframe)),
                    history=history_fingerprints.get((symbol, timeframe), ()),
                    barrier=barriers.get((symbol, timeframe)),
                )
                for (symbol, timeframe), close_time in last_processed.items()
            ]
        )
        return _TOP_STATE_OVERHEAD + sum(
            (
                _json_string_size(STATE_SCHEMA_VERSION),
                _json_string_size(MODULE_VERSION),
                _json_string_size(self._config_fingerprint),
                4 if bound_symbol is None else _json_string_size(bound_symbol),
                lifecycle_size,
                pre_evaluated_size,
                pre_candidate_size,
                cursor_size,
                zone_size,
            )
        )

    @staticmethod
    def _validate_pre_candidate_filter_traces(candidate: FvgPreCandidate, config: FvgConfig) -> None:
        if tuple(trace.name for trace in candidate.filters) != ("gap_size", "price", "volume"):
            raise FvgStateError("pre-FVG candidate filter trace set or order is invalid")
        if any(
            trace.decision not in {FvgFilterDecision.PASSED, FvgFilterDecision.SKIPPED} for trace in candidate.filters
        ):
            raise FvgStateError("pre-FVG candidate filter trace contains a non-candidate decision")

        gap_trace, price_trace, volume_trace = candidate.filters
        if gap_trace != evaluate_gap_size_filter(candidate.metrics, config):
            raise FvgStateError("pre-FVG candidate gap filter trace is inconsistent")

        price_applies = config.price_filter_target in {FvgFilterTarget.BOTH, FvgFilterTarget.PRE_FVG}
        if not price_applies:
            expected_price = FvgFilterTrace(
                name="price",
                decision=FvgFilterDecision.SKIPPED,
                reason="price_filter_target_not_applicable",
                metadata={
                    "configured_target": config.price_filter_target.value,
                    "event_target": FvgFilterTarget.PRE_FVG.value,
                },
            )
        else:
            raw_reference = price_trace.metadata.get("reference_price")
            if raw_reference is None:
                reference = None
            elif type(raw_reference) is str:
                try:
                    reference = parse_canonical_decimal_text(raw_reference, "pre-FVG price trace reference")
                except (TypeError, ValueError) as exc:
                    raise FvgStateError("pre-FVG price trace reference is invalid") from exc
                if not reference.is_finite() or reference <= 0:
                    raise FvgStateError("pre-FVG price trace reference must be finite and positive")
            else:
                raise FvgStateError("pre-FVG price trace reference must be a canonical string or null")
            validation_zone = FvgZone(
                zone_id=candidate.candidate_id,
                symbol=candidate.symbol,
                timeframe=candidate.timeframe,
                direction=candidate.direction,
                lower=candidate.lower,
                upper=candidate.upper,
                created_at=candidate.source_c_expected_close_time,
                source_a_open_time=candidate.source_a_open_time,
                source_b_open_time=candidate.source_b_open_time,
                source_c_open_time=candidate.source_c_open_time,
            )
            expected_price = evaluate_price_filter(validation_zone, config, reference_price=reference)
            if reference is not None:
                expected_distance = abs(candidate.midpoint - reference) / reference
                if candidate.metrics.distance_from_reference != expected_distance:
                    raise FvgStateError("pre-FVG candidate reference distance does not match price trace")
        if price_trace != expected_price:
            raise FvgStateError("pre-FVG candidate price filter trace is inconsistent")

        volume_applies = config.volume_filter_target in {FvgFilterTarget.BOTH, FvgFilterTarget.PRE_FVG}
        if not volume_applies:
            expected_volume = FvgFilterTrace(
                name="volume",
                decision=FvgFilterDecision.SKIPPED,
                reason="volume_filter_target_not_applicable",
                metadata={
                    "configured_target": config.volume_filter_target.value,
                    "event_target": FvgFilterTarget.PRE_FVG.value,
                },
            )
            if volume_trace != expected_volume:
                raise FvgStateError("pre-FVG candidate volume filter trace is inconsistent")
            if candidate.metrics.volume_ratio is not None:
                raise FvgStateError("pre-FVG candidate volume ratio must be unavailable for skipped target")
            return
        if config.volume_filter_mode is FvgVolumeFilterMode.DISABLED:
            expected_volume = FvgFilterTrace(
                name="volume",
                decision=FvgFilterDecision.SKIPPED,
                reason="volume_filter_disabled",
                metadata={"mode": FvgVolumeFilterMode.DISABLED.value},
            )
            if volume_trace != expected_volume:
                raise FvgStateError("pre-FVG candidate volume filter trace is inconsistent")
            if candidate.metrics.volume_ratio is not None:
                raise FvgStateError("pre-FVG candidate volume ratio must be unavailable when filter is disabled")
            return
        if volume_trace.decision is FvgFilterDecision.SKIPPED:
            allowed_reasons = {
                "volume_history_insufficient_allowed",
                "volume_data_missing_allowed",
                "volume_reference_zero_allowed",
            }
            if (
                config.volume_missing_policy is not FvgMissingDataPolicy.ALLOW
                or volume_trace.reason not in allowed_reasons
                or volume_trace.observed is not None
                or volume_trace.minimum is not None
                or volume_trace.maximum is not None
                or dict(volume_trace.metadata) != {"mode": config.volume_filter_mode.value}
                or candidate.metrics.volume_ratio is not None
            ):
                raise FvgStateError("pre-FVG candidate skipped volume trace is inconsistent")
            return
        if volume_trace.reason != "volume_within_bounds":
            raise FvgStateError("pre-FVG candidate volume trace reason is inconsistent")
        if volume_trace.observed != candidate.metrics.volume_ratio:
            raise FvgStateError("pre-FVG candidate volume ratio does not match volume filter trace")
        if volume_trace.minimum != config.volume_min_ratio or volume_trace.maximum != config.volume_max_ratio:
            raise FvgStateError("pre-FVG candidate volume trace bounds are inconsistent")
        expected_keys = {"mode", "lookback", "numerator", "reference"}
        if set(volume_trace.metadata) != expected_keys:
            raise FvgStateError("pre-FVG candidate volume trace metadata is inconsistent")
        if (
            volume_trace.metadata["mode"] != config.volume_filter_mode.value
            or volume_trace.metadata["lookback"] != config.volume_lookback
            or type(volume_trace.metadata["numerator"]) is not str
            or type(volume_trace.metadata["reference"]) is not str
        ):
            raise FvgStateError("pre-FVG candidate volume trace metadata is inconsistent")
        try:
            numerator = parse_canonical_decimal_text(
                volume_trace.metadata["numerator"],
                "pre-FVG candidate volume trace numerator",
            )
        except (TypeError, ValueError) as exc:
            raise FvgStateError("pre-FVG candidate volume trace numerator is invalid") from exc
        if not numerator.is_finite() or numerator < 0:
            raise FvgStateError("pre-FVG candidate volume trace numerator must be finite and non-negative")
        reference_text = volume_trace.metadata["reference"]
        if config.volume_filter_mode is FvgVolumeFilterMode.PERCENTILE:
            if reference_text != "percentile_rank":
                raise FvgStateError("pre-FVG candidate percentile volume trace is inconsistent")
            observed = volume_trace.observed
            if observed is None or not Decimal("0") <= observed <= Decimal("1"):
                raise FvgStateError("pre-FVG candidate percentile volume ratio must be in [0, 1]")
        else:
            try:
                reference = parse_canonical_decimal_text(reference_text, "pre-FVG candidate volume trace reference")
            except (TypeError, ValueError) as exc:
                raise FvgStateError("pre-FVG candidate volume trace reference is invalid") from exc
            if not reference.is_finite() or reference <= 0 or numerator / reference != volume_trace.observed:
                raise FvgStateError("pre-FVG candidate volume trace ratio is inconsistent")
