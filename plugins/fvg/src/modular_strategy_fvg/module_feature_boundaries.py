from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgFeatureBoundaryMixin:
    """Canonical external-candle boundary for feature construction."""

    @staticmethod
    def _feature_bar_identity(
        bar: CandleLike,
        label: str,
    ) -> tuple[str, str, datetime, datetime]:
        raw_symbol = _required_candle_attribute(bar, label, "symbol")
        raw_timeframe = _required_candle_attribute(bar, label, "timeframe")
        raw_open = _required_candle_attribute(bar, label, "timestamp")
        raw_close = _required_candle_attribute(bar, label, "close_time")
        if type(raw_symbol) is not str or type(raw_timeframe) is not str:
            raise TypeError(f"{label} candle symbol and timeframe must be canonical strings")
        if not isinstance(raw_open, datetime) or not isinstance(raw_close, datetime):
            raise TypeError(f"{label} candle timestamp and close_time must be datetimes")
        return (
            raw_symbol,
            raw_timeframe,
            as_utc(raw_open, f"{label}.timestamp"),
            as_utc(raw_close, f"{label}.close_time"),
        )

    def _pre_evaluation_feature(
        self,
        evaluation: FvgPreEvaluation,
        a: CandleLike,
        b: CandleLike,
        visible_until: datetime,
    ) -> Feature:
        a_symbol, a_timeframe, a_open, _ = self._feature_bar_identity(a, "pre-FVG source A")
        b_symbol, b_timeframe, b_open, b_close = self._feature_bar_identity(b, "pre-FVG source B")
        if a_symbol != b_symbol or a_timeframe != b_timeframe:
            raise ValueError("pre-FVG source candles must share symbol/timeframe")
        candidate = evaluation.candidate
        event_time = evaluation.target_evaluation_time or visible_until
        identity = "|".join(
            (
                b_symbol,
                b_timeframe,
                b_close.isoformat(),
                evaluation.status.value,
                evaluation.reason,
                self._config_fingerprint,
            )
        )
        feature_id = (
            candidate.candidate_id
            if candidate is not None
            else "pre-fvg:status:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        )
        return Feature(
            key=(PRE_CANDIDATE_KEY if candidate is not None else PRE_STATUS_KEY),
            source_module=self.name,
            created_at=min(event_time, visible_until),
            valid_until=(candidate.source_c_expected_close_time if candidate is not None else visible_until),
            value={
                **dict(evaluation.to_value()),
                "instrument": b_symbol,
                "timeframe": b_timeframe,
                "source_a_open_time": a_open.isoformat(),
                "source_b_open_time": b_open.isoformat(),
                "module_version": MODULE_VERSION,
                "schema_version": "1",
            },
            symbol=b_symbol,
            timeframe=b_timeframe,
            schema_version="1",
            feature_id=feature_id,
            invalidate_on_gap=False,
            metadata={
                "provisional": True,
                "pre_status": evaluation.status.value,
            },
        )

    def _pre_conversion_features(
        self,
        *,
        evaluation: FvgEvaluation | None,
        trigger_bar: CandleLike,
        visible_until: datetime,
        candidates: Mapping[str, FvgPreCandidate],
    ) -> tuple[tuple[Feature, ...], tuple[str, ...]]:
        symbol, timeframe, trigger_open, trigger_close = self._feature_bar_identity(trigger_bar, "pre-FVG resolution")
        matching = tuple(
            candidate
            for candidate in candidates.values()
            if candidate.symbol == symbol
            and candidate.timeframe == timeframe
            and candidate.source_c_open_time <= trigger_open
        )
        features: list[Feature] = []
        for candidate in sorted(matching, key=lambda item: item.candidate_id):
            if candidate.source_c_open_time < trigger_open:
                outcome = "confirmation_bar_missing"
                confirmed_zone_id = None
                evaluation_status = None
                evaluation_reason = "confirmation_bar_missing"
            elif (
                evaluation is not None
                and evaluation.zone is not None
                and evaluation.zone.direction is candidate.direction
            ):
                outcome = "confirmed"
                confirmed_zone_id = evaluation.zone.zone_id
                evaluation_status = evaluation.status.value
                evaluation_reason = evaluation.reason
            elif evaluation is not None and evaluation.zone is not None:
                outcome = "disappeared"
                confirmed_zone_id = None
                evaluation_status = evaluation.status.value
                evaluation_reason = evaluation.reason
            elif evaluation is not None and evaluation.status is FvgEvaluationStatus.UNAVAILABLE_WITH_CURRENT_DATA:
                outcome = "confirmed_unavailable"
                confirmed_zone_id = None
                evaluation_status = evaluation.status.value
                evaluation_reason = evaluation.reason
            elif evaluation is not None and evaluation.metrics is not None:
                outcome = "confirmed_filtered"
                confirmed_zone_id = None
                evaluation_status = evaluation.status.value
                evaluation_reason = evaluation.reason
            else:
                outcome = "disappeared"
                confirmed_zone_id = None
                evaluation_status = None if evaluation is None else evaluation.status.value
                evaluation_reason = None if evaluation is None else evaluation.reason
            event_id = f"{candidate.candidate_id}:resolved:{trigger_close.isoformat()}"
            features.append(
                Feature(
                    key=PRE_STATUS_KEY,
                    source_module=self.name,
                    created_at=min(trigger_close, visible_until),
                    valid_until=visible_until,
                    value={
                        "event_type": "pre_fvg_resolution",
                        "candidate_id": candidate.candidate_id,
                        "outcome": outcome,
                        "confirmed_zone_id": confirmed_zone_id,
                        "evaluation_status": evaluation_status,
                        "evaluation_reason": evaluation_reason,
                        "source_c_open_time": (candidate.source_c_open_time.isoformat()),
                        "source_c_close_time": (candidate.source_c_expected_close_time.isoformat()),
                        "resolved_at": trigger_close.isoformat(),
                    },
                    symbol=candidate.symbol,
                    timeframe=candidate.timeframe,
                    schema_version="1",
                    feature_id=event_id,
                    invalidate_on_gap=False,
                    metadata={
                        "provisional": False,
                        "pre_outcome": outcome,
                    },
                )
            )
        return (
            tuple(features),
            tuple(candidate.candidate_id for candidate in matching),
        )

    def _evaluation_feature(
        self,
        evaluation: FvgEvaluation,
        trigger_bar: CandleLike,
        visible_until: datetime,
    ) -> Feature:
        symbol, timeframe, trigger_open, trigger_close = self._feature_bar_identity(
            trigger_bar, "FVG evaluation trigger"
        )
        event_time = evaluation.evaluated_at or trigger_close
        identity = "|".join(
            (
                symbol,
                timeframe,
                trigger_open.isoformat(),
                evaluation.status.value,
                evaluation.reason,
                self._config_fingerprint,
            )
        )
        feature_id = "fvg:evaluation:" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        return Feature(
            key=EVALUATION_EVENT_KEY,
            source_module=self.name,
            created_at=min(event_time, visible_until),
            valid_until=visible_until,
            value={
                **dict(evaluation.to_value()),
                "instrument": symbol,
                "timeframe": timeframe,
                "source_name": self.config.source_name,
                "module_version": MODULE_VERSION,
                "schema_version": "1",
            },
            symbol=symbol,
            timeframe=timeframe,
            schema_version="1",
            feature_id=feature_id,
            invalidate_on_gap=False,
            metadata={"evaluation_status": evaluation.status.value},
        )

    def _lifecycle_feature(
        self,
        key: str,
        zone: FvgZone,
        visible_until: datetime,
        trigger_bar: CandleLike,
    ) -> Feature:
        _, _, trigger_open, trigger_close = self._feature_bar_identity(trigger_bar, "FVG lifecycle trigger")
        is_created = key == CREATED_EVENT_KEY
        suffix = "created" if is_created else f"invalidated:{trigger_close.isoformat()}"
        event_time = zone.created_at if is_created else trigger_close
        return Feature(
            key=key,
            source_module=self.name,
            created_at=min(event_time, visible_until),
            valid_until=visible_until,
            value={
                **zone.to_feature_value(),
                "event": "created" if is_created else "invalidated",
                "trigger_bar_open_time": trigger_open.isoformat(),
                "trigger_bar_close_time": trigger_close.isoformat(),
            },
            symbol=zone.symbol,
            timeframe=zone.timeframe,
            schema_version="1",
            feature_id=f"{zone.zone_id}:{suffix}",
            invalidate_on_gap=False,
            metadata={"lifecycle": suffix},
        )
