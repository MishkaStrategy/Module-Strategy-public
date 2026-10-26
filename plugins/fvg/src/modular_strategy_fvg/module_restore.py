from __future__ import annotations

from .module_shared import *  # noqa: F401,F403


class _FvgRestoreMixin:
    def snapshot_state(self) -> Mapping[str, object]:
        payload = self._state_payload()
        size = self._state_size(payload)
        if size > self.config.max_state_bytes:
            raise FvgStateError(f"FVG state is {size} bytes, exceeds configured limit {self.config.max_state_bytes}")
        return payload

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not isinstance(state, Mapping):
            raise FvgStateError("FVG state must be a mapping")
        thawed = _thaw_checkpoint_payload(state)
        if not isinstance(thawed, Mapping):
            raise FvgStateError("FVG state must be a mapping")
        size = self._state_size(thawed)
        if size > self.config.max_state_bytes:
            raise FvgStateError(f"FVG state is {size} bytes, exceeds configured limit {self.config.max_state_bytes}")

        raw_schema = thawed.get("state_schema_version")
        if type(raw_schema) is not str:
            raise FvgStateError("state_schema_version must be a canonical string")
        schema = raw_schema
        supported = {
            LEGACY_STATE_SCHEMA_VERSION,
            OLDER_STATE_SCHEMA_VERSION,
            PREVIOUS_STATE_SCHEMA_VERSION,
            STATE_SCHEMA_VERSION,
        }
        if schema not in supported:
            raise FvgStateError("unsupported FVG state schema version")

        legacy = schema == LEGACY_STATE_SCHEMA_VERSION
        older = schema == OLDER_STATE_SCHEMA_VERSION
        previous = schema == PREVIOUS_STATE_SCHEMA_VERSION
        current = schema == STATE_SCHEMA_VERSION

        base_keys = {
            "state_schema_version",
            "module_version",
            "config_fingerprint",
            "last_processed",
            "zones",
        }
        if legacy:
            expected_keys = base_keys
        elif current:
            expected_keys = base_keys | {
                "bound_symbol",
                "zone_lifecycle",
                "pre_evaluated",
                "pre_candidates",
            }
        else:
            expected_keys = base_keys | {"bound_symbol"}
        unknown = set(thawed) - expected_keys
        missing = expected_keys - set(thawed)
        if unknown or missing:
            raise FvgStateError(f"invalid FVG state keys; missing={sorted(missing)}, unknown={sorted(unknown)}")

        raw_module = thawed.get("module_version")
        raw_fingerprint = thawed.get("config_fingerprint")
        if type(raw_module) is not str or type(raw_fingerprint) is not str:
            raise FvgStateError("module_version and config_fingerprint must be canonical strings")
        if legacy:
            expected_module = LEGACY_MODULE_VERSION
            expected_fingerprint = self._legacy_config_fingerprint
        elif older:
            expected_module = OLDER_MODULE_VERSION
            expected_fingerprint = self._v2_config_fingerprint
        elif previous:
            expected_module = PREVIOUS_MODULE_VERSION
            expected_fingerprint = self._v3_config_fingerprint
            if not self._v3_compatible_configuration:
                raise FvgStateError("schema-3 FVG state cannot migrate into the configured 0.3.0 feature set")
        else:
            expected_module = MODULE_VERSION
            expected_fingerprint = self._config_fingerprint
        if raw_module != expected_module:
            raise FvgStateError("FVG module version mismatch")
        if raw_fingerprint != expected_fingerprint:
            raise FvgStateError("FVG state configuration mismatch")

        raw_last = thawed.get("last_processed")
        raw_zones = thawed.get("zones")
        if type(raw_last) is not list or type(raw_zones) is not list:
            raise FvgStateError("FVG state lists are malformed")
        if len(raw_last) > len(self.config.timeframes):
            raise FvgStateError("FVG state contains too many cursor rows")
        max_zones = len(self.config.timeframes) * self.config.max_active_zones_per_timeframe
        if len(raw_zones) > max_zones:
            raise FvgStateError("FVG state contains more zones than configured capacity")

        restored_last: dict[tuple[str, str], datetime] = {}
        restored_fingerprints: dict[tuple[str, str], str | None] = {}
        restored_history: dict[tuple[str, str], tuple[tuple[datetime, str], ...]] = {}
        restored_history_references: dict[tuple[str, str], tuple[_HistoryReference, ...]] = {}
        restored_signatures: dict[tuple[str, str], _BarSignature | None] = {}
        restored_barriers: dict[tuple[str, str], datetime] = {}
        symbols: set[str] = set()

        for raw_row in raw_last:
            if not isinstance(raw_row, Mapping):
                raise FvgStateError("last_processed row must be a mapping")
            row = _thaw_checkpoint_payload(raw_row)
            if not isinstance(row, Mapping):
                raise FvgStateError("last_processed row must be a mapping")
            if legacy:
                expected_row_keys = {"symbol", "timeframe", "close_time"}
            elif older:
                expected_row_keys = {
                    "symbol",
                    "timeframe",
                    "close_time",
                    "bar_fingerprint",
                    "barrier_close_time",
                }
            else:
                expected_row_keys = {
                    "symbol",
                    "timeframe",
                    "close_time",
                    "bar_fingerprint",
                    "history_fingerprints",
                    "barrier_close_time",
                }
            if set(row) != expected_row_keys:
                raise FvgStateError("last_processed row has invalid keys")
            if type(row["symbol"]) is not str or type(row["timeframe"]) is not str:
                raise FvgStateError("last_processed symbol and timeframe must be canonical strings")
            key = (row["symbol"], row["timeframe"])
            if not key[0]:
                raise FvgStateError("last_processed symbol must be non-empty")
            if key[1] not in self.config.timeframes:
                raise FvgStateError(f"last_processed contains unconfigured timeframe: {key[1]}")
            if key in restored_last:
                raise FvgStateError("duplicate last_processed key")
            cursor = self._parse_aware_datetime(row["close_time"], "last_processed.close_time")
            restored_last[key] = cursor
            symbols.add(key[0])

            fingerprint: str | None = None if legacy else row["bar_fingerprint"]
            if fingerprint is not None and (type(fingerprint) is not str or not _FINGERPRINT_RE.fullmatch(fingerprint)):
                raise FvgStateError("last_processed bar_fingerprint is invalid")
            restored_fingerprints[key] = fingerprint

            history: tuple[tuple[datetime, str], ...] = ()
            if previous or current:
                raw_history = row["history_fingerprints"]
                if type(raw_history) is not list:
                    raise FvgStateError("history_fingerprints must be a sequence")
                if len(raw_history) > _RECENT_FINGERPRINT_LIMIT:
                    raise FvgStateError("history_fingerprints exceeds the retained window")
                parsed_history: list[tuple[datetime, str]] = []
                for raw_history_row in raw_history:
                    if not isinstance(raw_history_row, Mapping):
                        raise FvgStateError("history_fingerprints row is malformed")
                    history_row = _thaw_checkpoint_payload(raw_history_row)
                    if not isinstance(history_row, Mapping) or set(history_row) != {"close_time", "bar_fingerprint"}:
                        raise FvgStateError("history_fingerprints row is malformed")
                    history_close = self._parse_aware_datetime(
                        history_row["close_time"],
                        "history_fingerprints.close_time",
                    )
                    history_fingerprint = history_row["bar_fingerprint"]
                    if type(history_fingerprint) is not str or not _FINGERPRINT_RE.fullmatch(history_fingerprint):
                        raise FvgStateError("history_fingerprints bar_fingerprint is invalid")
                    if parsed_history and history_close <= parsed_history[-1][0]:
                        raise FvgStateError("history_fingerprints must be strictly ordered")
                    if history_close > cursor:
                        raise FvgStateError("history_fingerprints cannot exceed its cursor")
                    parsed_history.append((history_close, history_fingerprint))
                history = tuple(parsed_history)
                if fingerprint is None and history:
                    raise FvgStateError("history_fingerprints require a current bar_fingerprint")
                if fingerprint is not None and (not history or history[-1] != (cursor, fingerprint)):
                    raise FvgStateError("current bar_fingerprint must match history_fingerprints tail")
            elif older and fingerprint is not None:
                history = ((cursor, fingerprint),)
            restored_history[key] = history
            restored_history_references[key] = ()
            restored_signatures[key] = None

            barrier: datetime | None = None
            if not legacy and row["barrier_close_time"] is not None:
                barrier = self._parse_aware_datetime(
                    row["barrier_close_time"],
                    "last_processed.barrier_close_time",
                )
            if legacy or older:
                barrier = cursor if barrier is None or barrier < cursor else barrier
            if barrier is not None:
                restored_barriers[key] = barrier

        restored_zones: dict[str, FvgZone] = {}
        counts: dict[tuple[str, str], int] = defaultdict(int)
        for raw_row in raw_zones:
            if not isinstance(raw_row, Mapping):
                raise FvgStateError("zone state row must be a mapping")
            row = _thaw_checkpoint_payload(raw_row)
            if not isinstance(row, Mapping):
                raise FvgStateError("zone state row must be a mapping")
            try:
                zone = FvgZone.from_state(row)
            except (ValueError, TypeError) as exc:
                raise FvgStateError(f"invalid zone state: {exc}") from exc
            expected_zone_id = make_zone_id(
                symbol=zone.symbol,
                timeframe=zone.timeframe,
                direction=zone.direction,
                source_c_open_time=zone.source_c_open_time,
                lower=zone.lower,
                upper=zone.upper,
            )
            if zone.zone_id != expected_zone_id:
                raise FvgStateError("zone_id does not match canonical zone identity")
            if zone.zone_id in restored_zones:
                raise FvgStateError("duplicate zone_id in state")
            if zone.timeframe not in self.config.timeframes:
                raise FvgStateError(f"state contains unconfigured timeframe: {zone.timeframe}")
            symbols.add(zone.symbol)
            zone_key = (zone.symbol, zone.timeframe)
            counts[zone_key] += 1
            if counts[zone_key] > self.config.max_active_zones_per_timeframe:
                raise FvgStateError("state exceeds active-zone capacity")
            zone_cursor = restored_last.get(zone_key)
            if zone_cursor is None or zone.created_at > zone_cursor:
                raise FvgStateError("active zone is not causally covered by its timeframe cursor")
            restored_zones[zone.zone_id] = zone

        if len(symbols) > 1:
            raise FvgStateError("FVG state contains multiple symbols for a single module instance")
        if legacy:
            bound_symbol = next(iter(symbols), None)
        else:
            raw_bound = thawed["bound_symbol"]
            if raw_bound is not None and type(raw_bound) is not str:
                raise FvgStateError("bound_symbol must be a string or null")
            bound_symbol = raw_bound
            if bound_symbol == "":
                raise FvgStateError("bound_symbol cannot be empty")
            if symbols and bound_symbol not in symbols:
                raise FvgStateError("bound_symbol does not match state contents")

        restored_lifecycle: dict[str, FvgZoneLifecycle] = {}
        restored_pre_candidates: dict[str, FvgPreCandidate] = {}
        restored_pre_evaluated: dict[tuple[str, str], datetime] = {}
        if current:
            raw_lifecycle = thawed["zone_lifecycle"]
            raw_pre_candidates = thawed["pre_candidates"]
            raw_pre_evaluated = thawed["pre_evaluated"]
            if (
                type(raw_lifecycle) is not list
                or type(raw_pre_candidates) is not list
                or type(raw_pre_evaluated) is not list
            ):
                raise FvgStateError("schema-4 lifecycle and pre-FVG state must be lists")
            if len(raw_lifecycle) != len(restored_zones):
                raise FvgStateError("schema-4 lifecycle rows must exactly cover active zones")
            if len(raw_pre_evaluated) > len(self.config.timeframes):
                raise FvgStateError("pre_evaluated contains too many timeframe rows")
            if len(raw_pre_candidates) > len(self.config.timeframes):
                raise FvgStateError("pre_candidates contains too many unresolved candidates")

            for raw_row in raw_lifecycle:
                if not isinstance(raw_row, Mapping):
                    raise FvgStateError("zone lifecycle row must be a mapping")
                row = _thaw_checkpoint_payload(raw_row)
                if not isinstance(row, Mapping):
                    raise FvgStateError("zone lifecycle row must be a mapping")
                try:
                    lifecycle = FvgZoneLifecycle.from_state(row)
                except (TypeError, ValueError) as exc:
                    raise FvgStateError(f"invalid zone lifecycle state: {exc}") from exc
                if lifecycle.zone_id in restored_lifecycle:
                    raise FvgStateError("duplicate zone lifecycle row in state")
                zone = restored_zones.get(lifecycle.zone_id)
                if zone is None:
                    raise FvgStateError("zone lifecycle does not belong to an active zone")
                if lifecycle.status in {
                    FvgLifecycleStatus.INVALIDATED,
                    FvgLifecycleStatus.EXPIRED,
                    FvgLifecycleStatus.ARCHIVED,
                    FvgLifecycleStatus.FULLY_FILLED,
                }:
                    raise FvgStateError("active zone cannot restore a terminal lifecycle")
                cursor = restored_last.get((zone.symbol, zone.timeframe))
                if cursor is None or lifecycle.last_observed_at > cursor:
                    raise FvgStateError("zone lifecycle is not causally covered by its cursor")
                if lifecycle.detected_at != zone.created_at:
                    raise FvgStateError("zone lifecycle detection time does not match zone")
                restored_lifecycle[lifecycle.zone_id] = lifecycle

            for raw_row in raw_pre_candidates:
                if not isinstance(raw_row, Mapping):
                    raise FvgStateError("pre-FVG candidate row must be a mapping")
                row = _thaw_checkpoint_payload(raw_row)
                if not isinstance(row, Mapping):
                    raise FvgStateError("pre-FVG candidate row must be a mapping")
                try:
                    candidate = FvgPreCandidate.from_state(row)
                except (TypeError, ValueError) as exc:
                    raise FvgStateError(f"invalid pre-FVG candidate: {exc}") from exc
                if candidate.metrics.distance_from_reference is None:
                    raise FvgStateError("pre-FVG candidate distance_from_reference must be available")
                if candidate.candidate_id in restored_pre_candidates:
                    raise FvgStateError("duplicate pre-FVG candidate_id in state")
                if candidate.timeframe not in self.config.timeframes:
                    raise FvgStateError(f"pre-FVG candidate uses unconfigured timeframe: {candidate.timeframe}")
                if not self.config.for_timeframe(candidate.timeframe).pre_fvg_enabled:
                    raise FvgStateError("pre-FVG candidate restored while pre-FVG is disabled")
                if bound_symbol is not None and candidate.symbol != bound_symbol:
                    raise FvgStateError("pre-FVG candidate symbol differs from bound_symbol")
                self._validate_pre_candidate_filter_traces(
                    candidate,
                    self.config.for_timeframe(candidate.timeframe),
                )
                canonical_candidate_id = make_pre_candidate_id(
                    symbol=candidate.symbol,
                    timeframe=candidate.timeframe,
                    direction=candidate.direction,
                    source_c_open_time=candidate.source_c_open_time,
                    evaluated_at=candidate.evaluated_at,
                    lower=candidate.lower,
                    upper=candidate.upper,
                )
                if candidate.candidate_id != canonical_candidate_id:
                    raise FvgStateError("pre-FVG candidate_id is not canonical")
                if candidate.source_c_expected_close_time <= candidate.processed_at:
                    raise FvgStateError("resolved pre-FVG candidate cannot be restored")
                restored_pre_candidates[candidate.candidate_id] = candidate
                symbols.add(candidate.symbol)

            for raw_row in raw_pre_evaluated:
                if not isinstance(raw_row, Mapping):
                    raise FvgStateError("pre_evaluated row must be a mapping")
                row = _thaw_checkpoint_payload(raw_row)
                if not isinstance(row, Mapping) or set(row) != {
                    "symbol",
                    "timeframe",
                    "source_c_open_time",
                }:
                    raise FvgStateError("pre_evaluated row is malformed")
                if type(row["symbol"]) is not str or type(row["timeframe"]) is not str:
                    raise FvgStateError("pre_evaluated identity must use canonical strings")
                key = (row["symbol"], row["timeframe"])
                if key in restored_pre_evaluated:
                    raise FvgStateError("duplicate pre_evaluated key")
                if key[1] not in self.config.timeframes:
                    raise FvgStateError(f"pre_evaluated uses unconfigured timeframe: {key[1]}")
                if not self.config.for_timeframe(key[1]).pre_fvg_enabled:
                    raise FvgStateError("pre_evaluated restored while pre-FVG is disabled")
                source_open = self._parse_aware_datetime(
                    row["source_c_open_time"],
                    "pre_evaluated.source_c_open_time",
                )
                if not is_period_aligned(source_open, key[1]):
                    raise FvgStateError("pre_evaluated source C open is not timeframe-aligned")
                cursor = restored_last.get(key)
                if cursor is None or source_open < cursor:
                    raise FvgStateError("pre_evaluated source C open is inconsistent with cursor")
                if bound_symbol is not None and key[0] != bound_symbol:
                    raise FvgStateError("pre_evaluated symbol differs from bound_symbol")
                restored_pre_evaluated[key] = source_open
                symbols.add(key[0])

            for candidate in restored_pre_candidates.values():
                key = (candidate.symbol, candidate.timeframe)
                if restored_pre_evaluated.get(key) != candidate.source_c_open_time:
                    raise FvgStateError("pre-FVG candidate is not covered by pre_evaluated state")
        else:
            restored_lifecycle = {zone_id: initial_lifecycle(zone) for zone_id, zone in restored_zones.items()}

        if len(symbols) > 1:
            raise FvgStateError("FVG state contains multiple symbols for a single module instance")
        if symbols and bound_symbol is None:
            raise FvgStateError("non-empty FVG state requires a bound_symbol")
        if symbols and bound_symbol not in symbols:
            raise FvgStateError("bound_symbol does not match restored state contents")

        migrated_payload = self._state_payload_from(
            zones=restored_zones,
            zone_lifecycle=restored_lifecycle,
            pre_candidates=restored_pre_candidates,
            pre_evaluated=restored_pre_evaluated,
            last_processed=restored_last,
            fingerprints=restored_fingerprints,
            history_fingerprints=restored_history,
            barriers=restored_barriers,
            bound_symbol=bound_symbol,
        )
        migrated_size = self._state_size(migrated_payload)
        if migrated_size > self.config.max_state_bytes:
            raise FvgStateError(
                f"migrated FVG state is {migrated_size} bytes, exceeds configured limit {self.config.max_state_bytes}"
            )

        self._last_processed = restored_last
        self._last_fingerprints = restored_fingerprints
        self._history_fingerprints = restored_history
        self._history_references = restored_history_references
        self._last_signatures = restored_signatures
        self._barriers = restored_barriers
        self._zones = restored_zones
        self._zone_lifecycle = restored_lifecycle
        self._pre_candidates = restored_pre_candidates
        self._pre_evaluated = restored_pre_evaluated
        self._bound_symbol = bound_symbol
        self._republish_pending = bool(restored_zones or restored_pre_candidates)
        self._state_size_bytes = migrated_size

    @property
    def active_zones(self) -> tuple[FvgZone, ...]:
        return tuple(self._zones[zone_id] for zone_id in sorted(self._zones))

    @property
    def pre_candidates(self) -> tuple[FvgPreCandidate, ...]:
        return tuple(self._pre_candidates[candidate_id] for candidate_id in sorted(self._pre_candidates))

    @property
    def bound_symbol(self) -> str | None:
        return self._bound_symbol
