from __future__ import annotations

import hashlib
import json
import logging
import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import RLock
from types import MappingProxyType
from typing import Any
from urllib.parse import quote

from modular_core import (
    Feature,
    FeatureDelete,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

from .config import (
    SessionBoundaryError,
    SessionRangeConfigurationError,
    SessionRangeSpec,
    parse_specs,
)
from .state import DECIMAL_CONTEXT, BarCursor, RangeAccumulator, StoredFeature

ACTIVE_FEATURE = "session_range.active"
COMPLETED_FEATURE = "session_range.completed"
STATE_SCHEMA_VERSION = "2"
MODULE_VERSION = "0.2.0"
_MAX_BOUNDS_CACHE = 4096
_MAX_TRACKED_STREAMS = 1024
_MAX_SCOPED_STATE = 65536

_SKIP_DUPLICATE = "session_range.duplicate_bar"
_SKIP_CONFLICTING_DUPLICATE = "session_range.conflicting_duplicate"
_SKIP_OUT_OF_ORDER = "session_range.out_of_order_bar"
_SKIP_UNKNOWN_INTERVAL = "session_range.unknown_or_incomplete_interval"
_SKIP_GAP = "session_range.data_gap"
_SKIP_MISSING_START = "session_range.missing_session_start"
_SKIP_BOUNDARY = "session_range.boundary_misaligned"
_SKIP_TOO_FEW_BARS = "session_range.minimum_bars_not_met"
_SKIP_RANGE_TOO_SMALL = "session_range.minimum_range_not_met"
_SKIP_RANGE_TOO_LARGE = "session_range.maximum_range_exceeded"
_SKIP_DST_BOUNDARY = "session_range.invalid_dst_boundary"
_SKIP_STREAM_LIMIT = "session_range.stream_limit_exceeded"
_SKIP_NUMERIC_OVERFLOW = "session_range.numeric_overflow"


class SessionRangeModule(StrategyModule):
    """Build deterministic OHLCV ranges for configurable local-time sessions.

    A completed feature is published only when every accepted bar is fully inside
    the configured half-open interval ``[start, end)`` and the final bar closes
    exactly on the session boundary. Partial boundary bars and in-session gaps
    invalidate the session rather than producing an optimistic range.
    """

    name = "session_range"

    def __init__(
        self,
        sessions: Sequence[Mapping[str, Any]],
        *,
        strict_data_validation: bool = False,
    ) -> None:
        if not isinstance(strict_data_validation, bool):
            raise SessionRangeConfigurationError("strict_data_validation must be boolean")
        self._specs = parse_specs(sessions)
        self._spec_by_name = {spec.name: spec for spec in self._specs}
        self._strict = strict_data_validation
        session_parameters = tuple(
            MappingProxyType(dict(spec.to_mapping())) for spec in self._specs
        )
        self.parameters = MappingProxyType(
            {
                "sessions": session_parameters,
                "strict_data_validation": strict_data_validation,
            }
        )
        canonical_config = json.dumps(
            {
                "sessions": [dict(spec.to_mapping()) for spec in self._specs],
                "strict_data_validation": strict_data_validation,
            },
            sort_keys=True,
            separators=(",", ":"),
            default=list,
        )
        self._config_fingerprint = hashlib.sha256(canonical_config.encode("utf-8")).hexdigest()
        self.manifest = ModuleManifest(
            name=self.name,
            version=MODULE_VERSION,
            api_version="1.0",
            required_timeframes=(),
            required_features=(),
            produced_features=(ACTIVE_FEATURE, COMPLETED_FEATURE),
            warmup_bars={},
            parameters_schema={
                "sessions": {"type": "array", "required": True},
                "strict_data_validation": {"type": "boolean", "default": False},
            },
            skip_until_ready=False,
            description="DST-aware, gap-safe configurable session range producer",
            state_schema_version=STATE_SCHEMA_VERSION,
        )
        self._active: dict[str, RangeAccumulator] = {}
        self._last_bar: dict[str, BarCursor] = {}
        self._terminal_token_by_scope: dict[str, str] = {}
        self._last_completed: dict[str, StoredFeature] = {}
        self._restore_pending = False
        self._bounds_cache: dict[tuple[str, date], tuple[datetime, datetime]] = {}
        self._last_prune_date: date | None = None
        self._lock = RLock()
        self._logger = logging.getLogger(__name__)

    def on_start(self) -> None:
        self._log(logging.INFO, "module_started", sessions=tuple(spec.name for spec in self._specs))

    def on_finish(self) -> None:
        self._log(logging.INFO, "module_finished", active_ranges=len(self._active))

    def on_bar(self, view: MarketView) -> ModuleDecision:
        with self._lock:
            bar = view.current
            events: list[dict[str, object]] = []
            diagnostics: dict[str, object] = {
                "bar_time": bar.timestamp.isoformat(),
                "symbol": bar.symbol,
                "timeframe": bar.timeframe,
                "events": events,
            }
            skips: list[str] = []
            features: list[Feature] = []
            feature_deletes: list[FeatureDelete] = []

            interval = bar.close_time - bar.timestamp
            if interval <= timedelta(0):
                return self._invalid_bar_decision(
                    _SKIP_UNKNOWN_INTERVAL,
                    diagnostics,
                    events,
                    features,
                    feature_deletes,
                    "candle timeframe does not define a positive closed interval",
                )

            stream = self._stream_key(bar.symbol, bar.timeframe)
            if stream not in self._last_bar and len(self._last_bar) >= _MAX_TRACKED_STREAMS:
                return self._invalid_bar_decision(
                    _SKIP_STREAM_LIMIT,
                    diagnostics,
                    events,
                    features,
                    feature_deletes,
                    f"tracked stream limit {_MAX_TRACKED_STREAMS} reached",
                )
            cursor = self._last_bar.get(stream)
            if view.data_gap:
                self._invalidate_stream(stream, _SKIP_GAP, events, feature_deletes)
                skips.append(_SKIP_GAP)
            if cursor is not None:
                current_fingerprint = (bar.open, bar.high, bar.low, bar.close, bar.volume)
                if bar.timestamp == cursor.open_time:
                    reason = (
                        _SKIP_DUPLICATE
                        if current_fingerprint == cursor.fingerprint
                        else _SKIP_CONFLICTING_DUPLICATE
                    )
                    if reason == _SKIP_CONFLICTING_DUPLICATE:
                        self._invalidate_stream(stream, reason, events, feature_deletes)
                    return self._invalid_bar_decision(
                        reason,
                        diagnostics,
                        events,
                        features,
                        feature_deletes,
                        "repeated candle timestamp",
                        prior_skips=skips,
                    )
                if bar.timestamp < cursor.open_time:
                    return self._invalid_bar_decision(
                        _SKIP_OUT_OF_ORDER,
                        diagnostics,
                        events,
                        features,
                        feature_deletes,
                        "candle timestamp moved backwards",
                        prior_skips=skips,
                    )
                if bar.timestamp != cursor.close_time and not view.data_gap:
                    self._invalidate_stream(stream, _SKIP_GAP, events, feature_deletes)
                    skips.append(_SKIP_GAP)

            self._last_bar[stream] = BarCursor.from_candle(bar)
            self._prune_terminal_state(bar.timestamp)

            reemitted = self._reemit_restored_features(view)
            features.extend(reemitted)
            if reemitted:
                events.append({"event": "restored_features_reemitted", "count": len(reemitted)})

            for spec in self._specs:
                try:
                    candidate = self._candidate_for_bar(spec, bar.timestamp, bar.close_time)
                except SessionBoundaryError as exc:
                    skips.append(f"{_SKIP_DST_BOUNDARY}:{spec.name}")
                    events.append(
                        {
                            "event": "session_boundary_rejected",
                            "session": spec.name,
                            "detail": str(exc),
                        }
                    )
                    continue
                if candidate is None:
                    continue
                anchor_date, start_utc, end_utc, fully_contained = candidate
                scope = self._scope_key(stream, spec.name)
                token = self._token(scope, anchor_date)

                if not fully_contained:
                    self._active.pop(token, None)
                    self._terminal_token_by_scope[scope] = token
                    skips.append(f"{_SKIP_BOUNDARY}:{spec.name}")
                    events.append(
                        {
                            "event": "boundary_misaligned",
                            "session": spec.name,
                            "session_date": anchor_date.isoformat(),
                        }
                    )
                    continue

                if self._terminal_token_by_scope.get(scope) == token:
                    continue

                state = self._active.get(token)
                if state is None:
                    if bar.timestamp != start_utc:
                        self._terminal_token_by_scope[scope] = token
                        skips.append(f"{_SKIP_MISSING_START}:{spec.name}")
                        events.append(
                            {
                                "event": "missing_session_start",
                                "session": spec.name,
                                "expected": start_utc.isoformat(),
                                "actual": bar.timestamp.isoformat(),
                            }
                        )
                        continue
                    state = RangeAccumulator.start(spec.name, anchor_date, start_utc, end_utc, bar)
                    self._active[token] = state
                    events.append({"event": "range_started", "session": spec.name, "token": token})
                else:
                    if bar.timestamp != state.next_open:
                        self._active.pop(token, None)
                        self._terminal_token_by_scope[scope] = token
                        skips.append(f"{_SKIP_GAP}:{spec.name}")
                        events.append(
                            {
                                "event": "in_session_gap",
                                "session": spec.name,
                                "expected": state.next_open.isoformat(),
                                "actual": bar.timestamp.isoformat(),
                            }
                        )
                        continue
                    state.push(bar)

                try:
                    if spec.emit_active and bar.close_time < end_utc:
                        features.append(self._active_feature(spec, state, bar.close_time))

                    if bar.close_time == end_utc:
                        completed, rejection = self._complete_feature(spec, state)
                        self._active.pop(token, None)
                        self._terminal_token_by_scope[scope] = token
                        if completed is not None:
                            features.append(completed)
                            self._last_completed[scope] = self._store_feature(completed)
                            events.append(
                                {
                                    "event": "range_completed",
                                    "session": spec.name,
                                    "session_date": anchor_date.isoformat(),
                                    "bars": state.bar_count,
                                    "width": str(state.width),
                                }
                            )
                            self._log(
                                logging.INFO,
                                "range_completed",
                                session=spec.name,
                                symbol=state.symbol,
                                session_date=anchor_date.isoformat(),
                                bars=state.bar_count,
                                width=str(state.width),
                            )
                        else:
                            if rejection is None:
                                raise RuntimeError(
                                    "session completion returned neither a feature "
                                    "nor a rejection reason"
                                )
                            skips.append(f"{rejection}:{spec.name}")
                            events.append(
                                {
                                    "event": "range_rejected",
                                    "session": spec.name,
                                    "reason": rejection,
                                }
                            )
                except (ArithmeticError, ValueError) as exc:
                    self._active.pop(token, None)
                    self._terminal_token_by_scope[scope] = token
                    reason = f"{_SKIP_NUMERIC_OVERFLOW}:{spec.name}"
                    skips.append(reason)
                    events.append(
                        {
                            "event": "numeric_range_rejected",
                            "session": spec.name,
                            "detail": str(exc),
                        }
                    )
                    self._log(
                        logging.WARNING,
                        "numeric_range_rejected",
                        session=spec.name,
                        detail=str(exc),
                    )
                    if self._strict:
                        raise ValueError(f"{reason}: {exc}") from exc

            diagnostics["active_ranges"] = len(self._active)
            diagnostics["features_emitted"] = len(features)
            return ModuleDecision(
                features=tuple(features),
                feature_deletes=tuple(feature_deletes),
                skips=tuple(dict.fromkeys(skips)),
                diagnostics=diagnostics,
            )

    def snapshot_state(self) -> Mapping[str, object]:
        with self._lock:
            return {
                "schema_version": STATE_SCHEMA_VERSION,
                "module_version": MODULE_VERSION,
                "config_fingerprint": self._config_fingerprint,
                "active": [self._active[key].to_mapping() for key in sorted(self._active)],
                "last_bars": {
                    key: self._last_bar[key].to_mapping() for key in sorted(self._last_bar)
                },
                "terminal_tokens": dict(sorted(self._terminal_token_by_scope.items())),
                "last_completed": {
                    key: self._last_completed[key].to_mapping()
                    for key in sorted(self._last_completed)
                },
            }

    def restore_state(self, state: Mapping[str, object]) -> None:
        with self._lock:
            if not isinstance(state, Mapping):
                raise ValueError("Session Range state must be a mapping")
            if not state:
                return
            if any(not isinstance(key, str) for key in state):
                raise ValueError("Session Range state keys must be strings")
            allowed_state_keys = {
                "schema_version",
                "module_version",
                "config_fingerprint",
                "active",
                "last_bars",
                "terminal_tokens",
                "last_completed",
            }
            unknown_state_keys = sorted(set(state) - allowed_state_keys)
            if unknown_state_keys:
                raise ValueError(
                    f"unknown Session Range state keys: {', '.join(unknown_state_keys)}"
                )
            schema_raw = state.get("schema_version", "")
            if not isinstance(schema_raw, str):
                raise ValueError("Session Range state schema_version must be a string")
            schema = schema_raw
            if schema not in {"1", STATE_SCHEMA_VERSION}:
                raise ValueError(f"unsupported Session Range state schema: {schema!r}")
            if schema == STATE_SCHEMA_VERSION:
                module_version = state.get("module_version")
                if not isinstance(module_version, str) or not module_version:
                    raise ValueError(
                        "Session Range schema-v2 state requires nonempty module_version"
                    )
            fingerprint = state.get("config_fingerprint")
            if schema == STATE_SCHEMA_VERSION and fingerprint is None:
                raise ValueError("Session Range schema-v2 state requires config_fingerprint")
            if fingerprint is not None and fingerprint != self._config_fingerprint:
                raise ValueError("Session Range state configuration fingerprint mismatch")
            active_raw = state.get("active", [])
            last_bars_raw = state.get("last_bars", {})
            terminal_raw = state.get("terminal_tokens", {})
            completed_raw = state.get("last_completed", {})
            if not isinstance(active_raw, (list, tuple)):
                raise ValueError("state.active must be a list or frozen tuple")
            if not all(isinstance(item, Mapping) for item in active_raw):
                raise ValueError("every state.active item must be a mapping")
            if not isinstance(last_bars_raw, Mapping):
                raise ValueError("state.last_bars must be a mapping")
            if not isinstance(terminal_raw, Mapping):
                raise ValueError("state.terminal_tokens must be a mapping")
            if not isinstance(completed_raw, Mapping):
                raise ValueError("state.last_completed must be a mapping")
            if len(active_raw) > _MAX_SCOPED_STATE:
                raise ValueError("state.active exceeds the bounded-state limit")
            if len(last_bars_raw) > _MAX_TRACKED_STREAMS:
                raise ValueError("state.last_bars exceeds the tracked-stream limit")
            if len(terminal_raw) > _MAX_SCOPED_STATE:
                raise ValueError("state.terminal_tokens exceeds the bounded-state limit")
            if len(completed_raw) > _MAX_SCOPED_STATE:
                raise ValueError("state.last_completed exceeds the bounded-state limit")

            restored_active: dict[str, RangeAccumulator] = {}
            for item in active_raw:
                accumulator = RangeAccumulator.from_mapping(item)
                if accumulator.session_name not in self._spec_by_name:
                    raise ValueError(
                        f"state references unknown session {accumulator.session_name!r}"
                    )
                stream = self._stream_key(accumulator.symbol, accumulator.timeframe)
                token = self._token(
                    self._scope_key(stream, accumulator.session_name), accumulator.anchor_date
                )
                if token in restored_active:
                    raise ValueError(f"duplicate active state token: {token}")
                restored_active[token] = accumulator

            for token, accumulator in restored_active.items():
                spec = self._spec_by_name[accumulator.session_name]
                expected_start, expected_end = spec.bounds_for(accumulator.anchor_date)
                if (accumulator.start_utc, accumulator.end_utc) != (expected_start, expected_end):
                    raise ValueError(f"state range boundaries do not match configuration: {token}")

            restored_last_bars: dict[str, BarCursor] = {}
            for key, value in last_bars_raw.items():
                if not isinstance(key, str) or not isinstance(value, Mapping):
                    raise ValueError("state.last_bars entries must map strings to mappings")
                restored_last_bars[key] = BarCursor.from_mapping(value)

            for token, accumulator in restored_active.items():
                stream = self._stream_key(accumulator.symbol, accumulator.timeframe)
                cursor = restored_last_bars.get(stream)
                if cursor is None or cursor.close_time != accumulator.next_open:
                    raise ValueError(f"active state cursor mismatch: {token}")

            restored_terminals: dict[str, str] = {}
            for key, value in terminal_raw.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    raise ValueError("state.terminal_tokens entries must be strings")
                session_name = key.rsplit("|", 1)[-1]
                if session_name not in self._spec_by_name:
                    raise ValueError("terminal token references an unknown session")
                try:
                    terminal_date = date.fromisoformat(value.rsplit("|", 1)[-1])
                except ValueError as exc:
                    raise ValueError("terminal token date is invalid") from exc
                if value != self._token(key, terminal_date):
                    raise ValueError("terminal token does not match its scope")
                restored_terminals[key] = value

            restored_completed: dict[str, StoredFeature] = {}
            for key, value in completed_raw.items():
                if not isinstance(key, str) or not isinstance(value, Mapping):
                    raise ValueError("state.last_completed entries must map strings to mappings")
                stored = StoredFeature.from_mapping(value)
                session_value = stored.metadata.get("session")
                if not isinstance(session_value, str):
                    raise ValueError("stored completed feature session must be a string")
                session_name = session_value
                value_session = stored.value.get("session")
                if value_session != session_name:
                    raise ValueError("stored completed feature session mismatch")
                if session_name not in self._spec_by_name:
                    raise ValueError(f"stored feature references unknown session {session_name!r}")
                expected_scope = self._scope_key(
                    self._stream_key(stored.symbol, stored.timeframe), session_name
                )
                if key != expected_scope or stored.key != COMPLETED_FEATURE:
                    raise ValueError("stored completed feature contract mismatch")
                if (
                    stored.metadata.get("complete") is not True
                    or stored.value.get("complete") is not True
                ):
                    raise ValueError("stored completed feature complete flag must be true")
                metadata_date = stored.metadata.get("session_date")
                value_date = stored.value.get("session_date")
                if metadata_date is not None and not isinstance(metadata_date, str):
                    raise ValueError("stored completed feature session_date must be a string")
                if value_date is not None and not isinstance(value_date, str):
                    raise ValueError("stored completed feature session_date must be a string")
                session_date_text = metadata_date or value_date or ""
                try:
                    session_date = date.fromisoformat(session_date_text)
                except ValueError as exc:
                    prefix = "schema-v1 " if schema == "1" else ""
                    raise ValueError(
                        f"{prefix}completed feature is missing a valid session_date"
                    ) from exc
                if (
                    metadata_date is not None
                    and value_date is not None
                    and metadata_date != value_date
                ):
                    raise ValueError("stored completed feature session_date mismatch")
                expected_feature_id = self._completed_feature_id(
                    session_name, stored.symbol, stored.timeframe, session_date
                )
                if schema == "1":
                    stored = replace(stored, feature_id=expected_feature_id)
                elif stored.feature_id != expected_feature_id:
                    raise ValueError("stored completed feature feature_id is not deterministic")
                restored_completed[key] = stored

            self._active = restored_active
            self._last_bar = restored_last_bars
            self._terminal_token_by_scope = restored_terminals
            self._last_completed = restored_completed
            self._restore_pending = True
            self._log(
                logging.INFO,
                "state_restored",
                active_ranges=len(self._active),
                completed_features=len(self._last_completed),
            )

    def _candidate_for_bar(
        self,
        spec: SessionRangeSpec,
        bar_start: datetime,
        bar_end: datetime,
    ) -> tuple[date, datetime, datetime, bool] | None:
        local_start = bar_start.astimezone(spec.timezone)
        anchors = (local_start.date(), local_start.date() - timedelta(days=1))
        for anchor in anchors:
            if anchor.weekday() not in spec.weekdays:
                continue
            start_utc, end_utc = self._bounds_for(spec, anchor)
            overlaps = bar_start < end_utc and bar_end > start_utc
            if not overlaps:
                continue
            fully_contained = bar_start >= start_utc and bar_end <= end_utc
            return anchor, start_utc, end_utc, fully_contained
        return None

    def _bounds_for(self, spec: SessionRangeSpec, anchor_date: date) -> tuple[datetime, datetime]:
        key = (spec.name, anchor_date)
        cached = self._bounds_cache.get(key)
        if cached is not None:
            return cached
        bounds = spec.bounds_for(anchor_date)
        if len(self._bounds_cache) >= _MAX_BOUNDS_CACHE:
            oldest = min(self._bounds_cache, key=lambda item: item[1])
            self._bounds_cache.pop(oldest, None)
        self._bounds_cache[key] = bounds
        return bounds

    def _active_feature(
        self, spec: SessionRangeSpec, state: RangeAccumulator, timestamp: datetime
    ) -> Feature:
        value = self._range_value(spec, state, complete=False)
        feature_id = (
            f"session-range-active:{spec.name}:{self._key_component(state.symbol)}:"
            f"{self._key_component(state.timeframe)}:{state.anchor_date.isoformat()}:"
            f"{timestamp.isoformat()}"
        )
        return Feature(
            key=ACTIVE_FEATURE,
            source_module=self.name,
            created_at=timestamp,
            valid_until=timestamp,
            value=value,
            symbol=state.symbol,
            timeframe=state.timeframe,
            schema_version="1",
            feature_id=feature_id,
            invalidate_on_gap=True,
            metadata={"session": spec.name, "complete": False},
        )

    def _complete_feature(
        self, spec: SessionRangeSpec, state: RangeAccumulator
    ) -> tuple[Feature | None, str | None]:
        if state.bar_count < spec.minimum_bars:
            return None, _SKIP_TOO_FEW_BARS
        if state.width < spec.minimum_range:
            return None, _SKIP_RANGE_TOO_SMALL
        if spec.maximum_range is not None and state.width > spec.maximum_range:
            return None, _SKIP_RANGE_TOO_LARGE
        next_start = spec.next_start_after(state.anchor_date)
        valid_until = max(state.end_utc, next_start - timedelta(microseconds=1))
        feature_id = self._completed_feature_id(
            spec.name, state.symbol, state.timeframe, state.anchor_date
        )
        return (
            Feature(
                key=COMPLETED_FEATURE,
                source_module=self.name,
                created_at=state.end_utc,
                valid_until=valid_until,
                value=self._range_value(spec, state, complete=True),
                symbol=state.symbol,
                timeframe=state.timeframe,
                schema_version="1",
                feature_id=feature_id,
                invalidate_on_gap=True,
                metadata={
                    "session": spec.name,
                    "session_date": state.anchor_date.isoformat(),
                    "complete": True,
                },
            ),
            None,
        )

    @staticmethod
    def _finite_float(value: Decimal, field_name: str) -> float:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError(f"{field_name} cannot be represented as a finite float")
        return result

    @classmethod
    def _range_value(
        cls, spec: SessionRangeSpec, state: RangeAccumulator, *, complete: bool
    ) -> dict[str, object]:
        width_pct = None
        if state.first_open != 0:
            width_pct = DECIMAL_CONTEXT.divide(state.width, state.first_open)
        start_local = state.start_utc.astimezone(spec.timezone)
        end_local = state.end_utc.astimezone(spec.timezone)
        return {
            "session": spec.name,
            "session_date": state.anchor_date.isoformat(),
            "timezone": spec.timezone_name,
            "start_utc": state.start_utc.isoformat(),
            "end_utc": state.end_utc.isoformat(),
            "start_local": start_local.isoformat(),
            "end_local": end_local.isoformat(),
            "open": cls._finite_float(state.first_open, "open"),
            "high": cls._finite_float(state.high, "high"),
            "low": cls._finite_float(state.low, "low"),
            "close": cls._finite_float(state.last_close, "close"),
            "midpoint": cls._finite_float(state.midpoint, "midpoint"),
            "width": cls._finite_float(state.width, "width"),
            "width_decimal": str(state.width),
            "width_pct": None if width_pct is None else cls._finite_float(width_pct, "width_pct"),
            "volume": cls._finite_float(state.volume, "volume"),
            "bar_count": state.bar_count,
            "complete": complete,
        }

    def _reemit_restored_features(self, view: MarketView) -> list[Feature]:
        if not self._restore_pending:
            return []
        self._restore_pending = False
        now = view.current.close_time
        existing_ids: set[str] = set()
        if view.features is not None:
            for item in view.features.get_all(COMPLETED_FEATURE, source_module=self.name):
                if item.feature_id is not None:
                    existing_ids.add(item.feature_id)
        result: list[Feature] = []
        for stored in self._last_completed.values():
            if stored.valid_until < now or stored.feature_id in existing_ids:
                continue
            result.append(
                Feature(
                    key=stored.key,
                    source_module=self.name,
                    created_at=stored.created_at,
                    valid_until=stored.valid_until,
                    value=stored.value,
                    symbol=stored.symbol,
                    timeframe=stored.timeframe,
                    schema_version="1",
                    feature_id=stored.feature_id,
                    invalidate_on_gap=True,
                    metadata=stored.metadata,
                )
            )
        return result

    @staticmethod
    def _store_feature(feature: Feature) -> StoredFeature:
        if feature.feature_id is None:
            raise ValueError("stored features require a deterministic feature_id")
        if feature.valid_until is None:
            raise ValueError("stored features require a finite valid_until timestamp")
        return StoredFeature(
            key=feature.key,
            feature_id=feature.feature_id,
            symbol=feature.symbol,
            timeframe=feature.timeframe or "base",
            created_at=feature.created_at,
            valid_until=feature.valid_until,
            value=dict(feature.value),
            metadata=dict(feature.metadata),
        )

    def _invalidate_stream(
        self,
        stream: str,
        reason: str,
        events: list[dict[str, object]],
        feature_deletes: list[FeatureDelete],
    ) -> None:
        removed: list[str] = []
        for token, state in tuple(self._active.items()):
            if self._stream_key(state.symbol, state.timeframe) != stream:
                continue
            self._active.pop(token, None)
            scope = self._scope_key(stream, state.session_name)
            self._terminal_token_by_scope[scope] = token
            removed.append(token)

        invalidated_features: list[str] = []
        for scope, stored in tuple(self._last_completed.items()):
            if self._stream_key(stored.symbol, stored.timeframe) != stream:
                continue
            self._last_completed.pop(scope, None)
            invalidated_features.append(stored.feature_id)
            feature_deletes.append(
                FeatureDelete(
                    key=stored.key,
                    source_module=self.name,
                    feature_id=stored.feature_id,
                    symbol=stored.symbol,
                    timeframe=stored.timeframe,
                    reason=reason,
                )
            )

        if removed or invalidated_features:
            events.append(
                {
                    "event": "stream_state_invalidated",
                    "reason": reason,
                    "tokens": tuple(removed),
                    "feature_ids": tuple(invalidated_features),
                }
            )
            self._log(
                logging.WARNING,
                "stream_state_invalidated",
                reason=reason,
                active_count=len(removed),
                feature_count=len(invalidated_features),
            )

    def _invalid_bar_decision(
        self,
        reason: str,
        diagnostics: dict[str, object],
        events: list[dict[str, object]],
        features: list[Feature],
        feature_deletes: list[FeatureDelete],
        detail: str,
        *,
        prior_skips: Sequence[str] = (),
    ) -> ModuleDecision:
        events.append({"event": "bar_rejected", "reason": reason, "detail": detail})
        self._log(logging.WARNING, "bar_rejected", reason=reason, detail=detail)
        if self._strict:
            raise ValueError(f"{reason}: {detail}")
        return ModuleDecision(
            features=tuple(features),
            feature_deletes=tuple(feature_deletes),
            skips=tuple(dict.fromkeys((*prior_skips, reason))),
            diagnostics=diagnostics,
        )

    def _prune_terminal_state(self, now: datetime) -> None:
        current_date = now.date()
        if self._last_prune_date == current_date:
            return
        self._last_prune_date = current_date
        cutoff = current_date - timedelta(days=16)
        for scope, token in tuple(self._terminal_token_by_scope.items()):
            anchor_text = token.rsplit("|", 1)[-1]
            try:
                anchor = date.fromisoformat(anchor_text)
            except ValueError:
                continue
            if anchor < cutoff:
                self._terminal_token_by_scope.pop(scope, None)
        for scope, stored in tuple(self._last_completed.items()):
            if stored.valid_until < now - timedelta(days=16):
                self._last_completed.pop(scope, None)
        for key in tuple(self._bounds_cache):
            if key[1] < cutoff:
                self._bounds_cache.pop(key, None)
        active_streams = {
            self._stream_key(state.symbol, state.timeframe) for state in self._active.values()
        }
        for stream, cursor in tuple(self._last_bar.items()):
            if stream not in active_streams and cursor.close_time < now - timedelta(days=16):
                self._last_bar.pop(stream, None)

    @staticmethod
    def _key_component(value: str) -> str:
        return quote(value, safe="._-")

    @classmethod
    def _completed_feature_id(
        cls, session_name: str, symbol: str, timeframe: str, anchor_date: date
    ) -> str:
        return (
            f"session-range-completed:{session_name}:{cls._key_component(symbol)}:"
            f"{cls._key_component(timeframe)}:{anchor_date.isoformat()}"
        )

    @classmethod
    def _stream_key(cls, symbol: str, timeframe: str) -> str:
        return f"{cls._key_component(symbol)}|{cls._key_component(timeframe)}"

    @staticmethod
    def _scope_key(stream: str, session_name: str) -> str:
        return f"{stream}|{session_name}"

    @staticmethod
    def _token(scope: str, anchor_date: date) -> str:
        return f"{scope}|{anchor_date.isoformat()}"

    def _log(self, level: int, event: str, **fields: object) -> None:
        self._logger.log(level, event, extra={"event": event, "module_name": self.name, **fields})
