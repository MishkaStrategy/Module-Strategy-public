from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgFeatureMixin:
    def _series_for(
        self,
        view: MarketView,
        timeframe: str,
    ) -> Sequence[CandleLike]:
        if timeframe == view.current.timeframe:
            return cast(Sequence[CandleLike], view.base_bars)
        return cast(
            Sequence[CandleLike],
            view.timeframe_bars.get(timeframe, ()),
        )

    @staticmethod
    def _increment_counter(
        diagnostics: dict[str, object],
        key: str,
        amount: int = 1,
    ) -> None:
        current = diagnostics.get(key, 0)
        if not isinstance(current, int):
            raise TypeError(f"diagnostic counter {key!r} is not an integer")
        diagnostics[key] = current + amount

    def _diagnostics(
        self,
        base_index: int,
        data_gap: bool,
        *,
        symbol: str | None = None,
        duplicates: int = 0,
    ) -> Mapping[str, object]:
        values: dict[str, object] = {
            "base_index": base_index,
            "data_gap": data_gap,
            "processed_bars": 0,
            "created": 0,
            "invalidated": 0,
            "expired": 0,
            "duplicates": duplicates,
            "non_consecutive_windows": 0,
            "capacity_evicted": 0,
            "evaluations_accepted": 0,
            "evaluations_rejected": 0,
            "evaluations_unavailable_with_current_data": 0,
            "evaluation_reasons": {},
            "pre_candidates": 0,
            "pre_rejected": 0,
            "pre_unavailable_with_current_data": 0,
            "pre_resolved": 0,
            "active_total": len(self._zones),
            "active_by_timeframe": dict(sorted(self._active_counts().items())),
        }
        if symbol is not None:
            values["symbol"] = symbol
        return MappingProxyType(values)

    @classmethod
    def _new_indexes(
        cls,
        series: Sequence[CandleLike],
        previous_close: datetime | None,
    ) -> range:
        return cls._new_indexes_canonical(series, previous_close)

    @staticmethod
    def _append_history_fingerprint(
        history: tuple[tuple[datetime, str], ...],
        close_time: datetime,
        fingerprint: str,
    ) -> tuple[tuple[datetime, str], ...]:
        canonical_close = as_utc(close_time, "history.close_time")
        updated = [item for item in history if item[0] != canonical_close]
        updated.append((canonical_close, fingerprint))
        updated.sort(key=lambda item: item[0])
        return tuple(updated[-_RECENT_FINGERPRINT_LIMIT:])

    @staticmethod
    def _append_history_reference(
        history: tuple[_HistoryReference, ...],
        close_time: datetime,
        bar: CandleLike,
    ) -> tuple[_HistoryReference, ...]:
        canonical_close = as_utc(close_time, "history.close_time")
        updated = [item for item in history if item[0] != canonical_close]
        updated.append((canonical_close, bar))
        updated.sort(key=lambda item: item[0])
        return tuple(updated[-_RECENT_FINGERPRINT_LIMIT:])

    def _is_invalidated_values(
        self,
        zone: FvgZone,
        *,
        bar_low: Decimal,
        bar_high: Decimal,
        bar_close: Decimal,
    ) -> bool:
        if zone.direction is FvgDirection.BULLISH:
            observed = bar_low if self.config.invalidation_basis is FvgInvalidationBasis.WICK else bar_close
            threshold = {
                FvgInvalidationLevel.FIRST_TOUCH: zone.upper,
                FvgInvalidationLevel.MIDPOINT: zone.midpoint,
                FvgInvalidationLevel.FULL_FILL: zone.lower,
            }[self.config.invalidation_level]
            return observed <= threshold
        observed = bar_high if self.config.invalidation_basis is FvgInvalidationBasis.WICK else bar_close
        threshold = {
            FvgInvalidationLevel.FIRST_TOUCH: zone.lower,
            FvgInvalidationLevel.MIDPOINT: zone.midpoint,
            FvgInvalidationLevel.FULL_FILL: zone.upper,
        }[self.config.invalidation_level]
        return observed >= threshold

    def _enforce_capacity(
        self,
        zones: dict[str, FvgZone],
        symbol: str,
        timeframe: str,
    ) -> tuple[FvgZone, ...]:
        matching = sorted(
            (zone for zone in zones.values() if zone.symbol == symbol and zone.timeframe == timeframe),
            key=lambda zone: (zone.created_at, zone.zone_id),
        )
        excess = len(matching) - self.config.max_active_zones_per_timeframe
        if excess <= 0:
            return ()
        removed: list[FvgZone] = []
        for zone in matching[:excess]:
            removed.append(zones.pop(zone.zone_id))
        return tuple(removed)

    def _active_feature(self, zone: FvgZone) -> Feature:
        return Feature(
            key=ACTIVE_ZONE_KEY,
            source_module=self.name,
            created_at=zone.created_at,
            value=zone.to_feature_value(),
            symbol=zone.symbol,
            timeframe=zone.timeframe,
            schema_version="1",
            feature_id=zone.zone_id,
            invalidate_on_gap=True,
            metadata={"lifecycle": "active"},
        )

    def _zone_state_feature(
        self,
        zone: FvgZone,
        lifecycle: FvgZoneLifecycle,
        visible_until: datetime,
    ) -> Feature:
        event_time = lifecycle.last_observed_at
        event_id = f"{zone.zone_id}:state:{event_time.isoformat()}:{lifecycle.status.value}:{lifecycle.contact_count}"
        return Feature(
            key=ZONE_STATE_KEY,
            source_module=self.name,
            created_at=min(event_time, visible_until),
            valid_until=visible_until,
            value={
                **dict(zone.to_feature_value()),
                **dict(lifecycle.to_value()),
            },
            symbol=zone.symbol,
            timeframe=zone.timeframe,
            schema_version="1",
            feature_id=event_id,
            invalidate_on_gap=False,
            metadata={"lifecycle": lifecycle.status.value},
        )

    def _restored_pre_candidate_feature(
        self,
        candidate: FvgPreCandidate,
    ) -> Feature:
        return Feature(
            key=PRE_CANDIDATE_KEY,
            source_module=self.name,
            created_at=candidate.evaluated_at,
            valid_until=candidate.source_c_expected_close_time,
            value={
                "status": FvgPreStatus.CANDIDATE.value,
                "reason": "pre_fvg_candidate",
                "event_type": "pre_fvg",
                "target_evaluation_time": candidate.evaluated_at.isoformat(),
                "processed_at": candidate.processed_at.isoformat(),
                "metrics": dict(candidate.metrics.to_value()),
                "filters": tuple(dict(item.to_value()) for item in candidate.filters),
                "candidate": dict(candidate.to_value()),
                "instrument": candidate.symbol,
                "timeframe": candidate.timeframe,
                "source_a_open_time": (candidate.source_a_open_time.isoformat()),
                "source_b_open_time": (candidate.source_b_open_time.isoformat()),
                "module_version": MODULE_VERSION,
                "schema_version": "1",
            },
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            schema_version="1",
            feature_id=candidate.candidate_id,
            invalidate_on_gap=False,
            metadata={
                "provisional": True,
                "pre_status": FvgPreStatus.CANDIDATE.value,
            },
        )

    @staticmethod
    def _pre_candidate_delete(
        candidate: FvgPreCandidate,
        reason: FvgReasonCode,
    ) -> FeatureDelete:
        return FeatureDelete(
            key=PRE_CANDIDATE_KEY,
            source_module="fvg",
            feature_id=candidate.candidate_id,
            symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            reason=reason.value,
        )
