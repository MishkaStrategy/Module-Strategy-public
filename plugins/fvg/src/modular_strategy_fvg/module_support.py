from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgSupportMixin:
    def _handle_order_violation(
        self,
        *,
        key: tuple[str, str],
        latest_close: datetime,
        previous_close: datetime,
        zones: dict[str, FvgZone],
        last_processed: dict[tuple[str, str], datetime],
        fingerprints: dict[tuple[str, str], str | None],
        history_fingerprints: dict[tuple[str, str], tuple[tuple[datetime, str], ...]],
        history_references: dict[tuple[str, str], tuple[_HistoryReference, ...]],
        signatures: dict[tuple[str, str], _BarSignature | None],
        barriers: dict[tuple[str, str], datetime],
        skips: list[str],
    ) -> tuple[FvgZone, ...]:
        if self.config.order_violation_policy is FvgOrderViolationPolicy.RAISE:
            raise FvgDataOrderError(
                f"timeframe {key[1]} regressed from {previous_close.isoformat()} to {latest_close.isoformat()}"
            )
        symbol, timeframe = key
        removed: list[FvgZone] = []
        for zone_id, zone in tuple(zones.items()):
            if zone.symbol == symbol and zone.timeframe == timeframe:
                removed.append(zones.pop(zone_id))
        last_processed[key] = latest_close
        fingerprints.pop(key, None)
        history_fingerprints.pop(key, None)
        history_references.pop(key, None)
        signatures.pop(key, None)
        barriers[key] = latest_close
        skips.append(self._order_reset_reason(timeframe))
        return tuple(sorted(removed, key=lambda item: item.zone_id))

    @staticmethod
    def _clear_pre_timeframe_scope(
        symbol: str,
        timeframe: str,
        candidates: dict[str, FvgPreCandidate],
        evaluated: dict[tuple[str, str], datetime],
    ) -> tuple[FvgPreCandidate, ...]:
        removed: list[FvgPreCandidate] = []
        for candidate_id, candidate in tuple(candidates.items()):
            if candidate.symbol == symbol and candidate.timeframe == timeframe:
                removed.append(candidates.pop(candidate_id))
        evaluated.pop((symbol, timeframe), None)
        return tuple(sorted(removed, key=lambda item: item.candidate_id))

    @staticmethod
    def _clear_symbol(
        symbol: str,
        zones: dict[str, FvgZone],
        last_processed: dict[tuple[str, str], datetime],
        fingerprints: dict[tuple[str, str], str | None],
        history_fingerprints: dict[tuple[str, str], tuple[tuple[datetime, str], ...]],
        history_references: dict[tuple[str, str], tuple[_HistoryReference, ...]],
        signatures: dict[tuple[str, str], _BarSignature | None],
        barriers: dict[tuple[str, str], datetime],
    ) -> tuple[FvgZone, ...]:
        removed: list[FvgZone] = []
        for zone_id, zone in tuple(zones.items()):
            if zone.symbol == symbol:
                removed.append(zones.pop(zone_id))
        keys = (
            set(last_processed)
            | set(fingerprints)
            | set(history_fingerprints)
            | set(history_references)
            | set(signatures)
            | set(barriers)
        )
        for key in tuple(keys):
            if key[0] == symbol:
                last_processed.pop(key, None)
                fingerprints.pop(key, None)
                history_fingerprints.pop(key, None)
                history_references.pop(key, None)
                signatures.pop(key, None)
                barriers.pop(key, None)
        return tuple(sorted(removed, key=lambda item: item.zone_id))

    def _active_counts(self) -> Mapping[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for zone in self._zones.values():
            counts[zone.timeframe] += 1
        return counts

    @staticmethod
    def _delete(zone: FvgZone, reason: FvgReasonCode) -> FeatureDelete:
        return FeatureDelete(
            key=ACTIVE_ZONE_KEY,
            source_module="fvg",
            feature_id=zone.zone_id,
            symbol=zone.symbol,
            timeframe=zone.timeframe,
            reason=reason.value,
        )

    @staticmethod
    def _parse_aware_datetime(value: object, name: str) -> datetime:
        try:
            return parse_canonical_utc_datetime(value, name)
        except (TypeError, ValueError) as exc:
            raise FvgStateError(str(exc)) from exc

    @staticmethod
    def _fingerprint_parameters(parameters: Mapping[str, object]) -> str:
        def json_value(value: object) -> object:
            if isinstance(value, Mapping):
                return {key: json_value(item) for key, item in value.items()}
            if isinstance(value, (list, tuple)):
                return [json_value(item) for item in value]
            return value

        payload = json.dumps(
            json_value(parameters),
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @staticmethod
    def _state_size(state: Mapping[str, object]) -> int:
        try:
            payload = json.dumps(
                state,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
            )
        except (TypeError, ValueError) as exc:
            raise FvgStateError("FVG state must be JSON-serializable") from exc
        return len(payload.encode("utf-8"))

    @staticmethod
    def _raw_value_signature(value: object) -> object:
        if value is None:
            return None
        if type(value) is float:
            return ("float", value.hex())
        if type(value) is int:
            return ("int", value)
        parsed = as_decimal(value)  # type: ignore[arg-type]
        return ("decimal", canonical_decimal_text(parsed))

    @staticmethod
    def _raw_signature(
        bar: CandleLike,
        bar_close: datetime,
    ) -> _BarSignature:
        canonical_close = as_utc(bar_close, "signature.close_time")
        if isinstance(bar, Candle):
            return (
                bar.symbol,
                bar.timeframe,
                as_utc(bar.timestamp, "signature.timestamp"),
                canonical_close,
                _FvgSupportMixin._raw_value_signature(bar.open),
                _FvgSupportMixin._raw_value_signature(bar.high),
                _FvgSupportMixin._raw_value_signature(bar.low),
                _FvgSupportMixin._raw_value_signature(bar.close),
                _FvgSupportMixin._raw_value_signature(bar.volume),
            )
        label = "signature"
        raw_symbol = _required_candle_attribute(bar, label, "symbol")
        raw_timeframe = _required_candle_attribute(bar, label, "timeframe")
        raw_timestamp = _required_candle_attribute(bar, label, "timestamp")
        if type(raw_symbol) is not str or type(raw_timeframe) is not str:
            raise TypeError("signature candle symbol and timeframe must be canonical strings")
        if not isinstance(raw_timestamp, datetime):
            raise TypeError("signature candle timestamp must be a datetime")
        return (
            raw_symbol,
            raw_timeframe,
            as_utc(raw_timestamp, "signature.timestamp"),
            canonical_close,
            _FvgSupportMixin._raw_value_signature(_optional_candle_attribute(bar, label, "open")),
            _FvgSupportMixin._raw_value_signature(_required_candle_attribute(bar, label, "high")),
            _FvgSupportMixin._raw_value_signature(_required_candle_attribute(bar, label, "low")),
            _FvgSupportMixin._raw_value_signature(_required_candle_attribute(bar, label, "close")),
            _FvgSupportMixin._raw_value_signature(_optional_candle_attribute(bar, label, "volume")),
        )

    @staticmethod
    def _conflicting_duplicate_error(
        key: tuple[str, str],
        close_time: datetime,
    ) -> FvgDataConflictError:
        return FvgDataConflictError(f"conflicting duplicate for {key[0]} {key[1]} at {close_time.isoformat()}")

    @staticmethod
    def _no_data_reason(timeframe: str) -> str:
        return f"fvg.no_data:{timeframe}"

    @staticmethod
    def _order_reset_reason(timeframe: str) -> str:
        return f"fvg.order_violation_reset:{timeframe}"
