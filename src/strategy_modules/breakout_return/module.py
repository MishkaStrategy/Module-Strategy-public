from __future__ import annotations

import hashlib
import json
import logging
import re
from collections import deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import cast

from modular_core import (
    Candle,
    Feature,
    FeatureDelete,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    SetupEvent,
    SetupStatus,
    Side,
    Signal,
    StrategyModule,
)

from .config import (
    BreakoutConfirmation,
    BreakoutReturnConfig,
    DirectionMode,
    Numeric,
    RangeChangePolicy,
    ReferenceMode,
    ReturnConfirmation,
)
from .models import (
    ActiveBreakout,
    BreakoutDirection,
    BreakoutReturnCounters,
    BreakoutReturnDataError,
    BreakoutReturnPhase,
    BreakoutReturnReason,
    BreakoutReturnStateError,
    ReferenceRange,
)

LOGGER = logging.getLogger(__name__)
_BPS = Decimal("10000")
_STATE_VERSION = 2
_LEGACY_STATE_VERSION = 1
_SAFE_ID = re.compile(r"[^A-Za-z0-9_.-]+")
_TIMEFRAME_RE = re.compile(r"^(\d+)([smhdw])$")

_STATE_KEYS_V1 = frozenset(
    {
        "payload_version",
        "sequence",
        "history_count",
        "maxima",
        "minima",
        "active",
        "last_bar_time",
        "last_bar_signature",
        "bound_symbol",
        "bound_timeframe",
    }
)
_STATE_KEYS_V2 = _STATE_KEYS_V1 | {"config_fingerprint", "counters"}
_ACTIVE_KEYS = frozenset(
    {
        "setup_id",
        "direction",
        "signal_side",
        "reference",
        "breakout_time",
        "breakout_sequence",
        "extreme",
        "symbol",
        "timeframe",
        "feature_revision",
    }
)
_REFERENCE_KEYS = frozenset({"high", "low", "identifier", "source", "created_at"})

_BarSignature = tuple[datetime, Decimal, Decimal, Decimal, Decimal, Decimal, str, str]


@dataclass(frozen=True, slots=True)
class _BarValues:
    timestamp: datetime
    close_time: datetime
    symbol: str
    timeframe: str
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    signature: _BarSignature


@dataclass(frozen=True, slots=True)
class _ReferenceResolution:
    reference: ReferenceRange | None
    reason: BreakoutReturnReason | None = None
    details: Mapping[str, object] = MappingProxyType({})


class BreakoutReturnModule(StrategyModule):
    """Detect a breakout followed by a controlled return into its reference range.

    The module publishes analytical facts only. It does not place orders or own
    risk, protection, target, portfolio, or execution responsibilities.

    Above-range breakout + return emits ``SELL``; below-range breakout + return
    emits ``BUY``. Every decision is based on the current completed bar and
    point-in-time features exposed by :class:`MarketView`.
    """

    name = "breakout_return"
    ACTIVE_FEATURE = "breakout_return.active_setup"
    CONFIRMED_FEATURE = "breakout_return.confirmed"
    PRODUCED_FEATURES = (ACTIVE_FEATURE, CONFIRMED_FEATURE)

    _PARAMETERS_SCHEMA: Mapping[str, Mapping[str, object]] = MappingProxyType(
        {
            "reference_mode": {"type": "string", "enum": ("rolling", "feature")},
            "reference_feature_key": {"type": "string"},
            "reference_feature_timeframe": {"type": "string"},
            "reference_feature_schema_version": {"type": "string"},
            "max_reference_age_seconds": {
                "type": "integer",
                "minimum": 1,
                "maximum": 10_000_000_000,
            },
            "lookback_bars": {"type": "integer", "minimum": 2, "maximum": 100_000},
            "direction": {"type": "string", "enum": ("both", "above", "below")},
            "breakout_confirmation": {"type": "string", "enum": ("close", "body")},
            "return_confirmation": {"type": "string", "enum": ("close", "body")},
            "breakout_buffer_bps": {"type": "number", "minimum": 0, "maximum": 5_000},
            "min_return_depth": {"type": "number", "minimum": 0, "maximum": 1},
            "max_return_depth": {"type": "number", "minimum": 0, "maximum": 1},
            "max_return_bars": {"type": "integer", "minimum": 1, "maximum": 100_000},
            "range_change_policy": {
                "type": "string",
                "enum": ("invalidate", "keep_snapshot"),
            },
        }
    )

    def __init__(
        self,
        *,
        reference_mode: str = "rolling",
        reference_feature_key: str = "range.current",
        reference_feature_timeframe: str | None = None,
        reference_feature_schema_version: str | None = None,
        max_reference_age_seconds: int | None = None,
        lookback_bars: int = 20,
        direction: str = "both",
        breakout_confirmation: str = "body",
        return_confirmation: str = "body",
        breakout_buffer_bps: Numeric = 0.0,
        min_return_depth: Numeric = 0.02,
        max_return_depth: Numeric = 0.20,
        max_return_bars: int = 36,
        range_change_policy: str = "invalidate",
    ) -> None:
        try:
            self.config = BreakoutReturnConfig(
                reference_mode=ReferenceMode(reference_mode),
                reference_feature_key=reference_feature_key,
                reference_feature_timeframe=reference_feature_timeframe,
                reference_feature_schema_version=reference_feature_schema_version,
                max_reference_age_seconds=max_reference_age_seconds,
                lookback_bars=lookback_bars,
                direction=DirectionMode(direction),
                breakout_confirmation=BreakoutConfirmation(breakout_confirmation),
                return_confirmation=ReturnConfirmation(return_confirmation),
                breakout_buffer_bps=breakout_buffer_bps,
                min_return_depth=min_return_depth,
                max_return_depth=max_return_depth,
                max_return_bars=max_return_bars,
                range_change_policy=RangeChangePolicy(range_change_policy),
            )
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid breakout_return configuration: {exc}") from exc

        self.parameters = self.config.as_parameters()
        required_features = (
            (self.config.reference_feature_key,)
            if self.config.reference_mode is ReferenceMode.FEATURE
            else ()
        )
        self.manifest = ModuleManifest(
            name=self.name,
            version="1.0.0",
            api_version="1.0",
            required_features=required_features,
            produced_features=self.PRODUCED_FEATURES,
            warmup_bars={},
            parameters_schema=self._PARAMETERS_SCHEMA,
            skip_until_ready=False,
            description=(
                "Stateful closed-bar breakout and controlled-return detector. "
                "Produces setups, signals and immutable features; never places orders."
            ),
            state_schema_version="1",
        )

        buffer_fraction = (
            self._decimal(
                self.config.breakout_buffer_bps,
                "breakout_buffer_bps",
                positive=False,
            )
            / _BPS
        )
        self._upper_breakout_factor = Decimal(1) + buffer_fraction
        self._lower_breakout_factor = Decimal(1) - buffer_fraction
        self._min_return_depth = self._decimal(
            self.config.min_return_depth,
            "min_return_depth",
            positive=False,
        )
        self._max_return_depth = self._decimal(
            self.config.max_return_depth,
            "max_return_depth",
            positive=False,
        )
        self._allow_above = self.config.direction in {DirectionMode.BOTH, DirectionMode.ABOVE}
        self._allow_below = self.config.direction in {DirectionMode.BOTH, DirectionMode.BELOW}
        self._breakout_requires_body = (
            self.config.breakout_confirmation is BreakoutConfirmation.BODY
        )
        self._return_requires_body = self.config.return_confirmation is ReturnConfirmation.BODY
        self._config_fingerprint = self._build_config_fingerprint()

        self._sequence = 0
        self._history_count = 0
        self._maxima: deque[tuple[int, Decimal]] = deque()
        self._minima: deque[tuple[int, Decimal]] = deque()
        self._active: ActiveBreakout | None = None
        self._last_bar_time: datetime | None = None
        self._last_bar_signature: _BarSignature | None = None
        self._bound_symbol: str | None = None
        self._bound_timeframe: str | None = None
        self._cached_timeframe: str | None = None
        self._cached_close_delta = timedelta(0)
        self._restored = False
        self._counters = BreakoutReturnCounters()

    @property
    def phase(self) -> BreakoutReturnPhase:
        return (
            BreakoutReturnPhase.WAITING_RETURN
            if self._active is not None
            else BreakoutReturnPhase.SCANNING
        )

    @property
    def diagnostic_counters(self) -> Mapping[str, int]:
        """Return a bounded immutable snapshot of operational counters."""

        return self._counters.as_mapping()

    @property
    def config_fingerprint(self) -> str:
        """Deterministic fingerprint embedded into state payload version 2."""

        return self._config_fingerprint

    def on_start(self) -> None:
        LOGGER.debug(
            "breakout_return_started",
            extra={
                "module_name": self.name,
                "restored": self._restored,
                "phase": self.phase.value,
                "config_fingerprint": self._config_fingerprint,
            },
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        values = self._bar_values(view.current)
        duplicate = self._validate_stream(values)
        if duplicate:
            self._counters.events_seen += 1
            self._counters.duplicate_events += 1
            return ModuleDecision(
                skips=(BreakoutReturnReason.DUPLICATE_EVENT_IGNORED.value,),
                diagnostics=self._diagnostics(
                    "duplicate_event_ignored",
                    timestamp=values.timestamp.isoformat(),
                ),
            )

        current_sequence = self._sequence + 1
        self._counters.events_seen += 1
        self._counters.bars_processed += 1

        if view.data_gap:
            self._counters.data_gaps += 1
            setup_events: tuple[SetupEvent, ...] = ()
            deletes: tuple[FeatureDelete, ...] = ()
            if self._active is not None:
                setup_events = (
                    self._setup_terminal(
                        SetupStatus.INVALIDATED,
                        BreakoutReturnReason.DATA_GAP_RESET,
                    ),
                )
                deletes = (self._active_delete("data_gap"),)
                self._counters.invalidated += 1
                self._active = None
            self._reset_rolling_window()
            self._complete_bar(values, current_sequence)
            return ModuleDecision(
                setups=setup_events,
                feature_deletes=deletes,
                skips=(BreakoutReturnReason.DATA_GAP_RESET.value,),
                diagnostics=self._diagnostics(
                    "data_gap_reset",
                    sequence=current_sequence,
                ),
            )

        if (
            self._active is not None
            and self.config.reference_mode is ReferenceMode.FEATURE
            and self.config.range_change_policy is RangeChangePolicy.INVALIDATE
        ):
            resolution = self._feature_reference(view, values)
            mismatch_reason = self._reference_mismatch_reason(resolution)
            if mismatch_reason is not None:
                terminal = self._setup_terminal(SetupStatus.INVALIDATED, mismatch_reason)
                deletion = self._active_delete(mismatch_reason.value)
                self._counters.invalidated += 1
                self._active = None
                self._complete_bar(values, current_sequence)
                return ModuleDecision(
                    setups=(terminal,),
                    feature_deletes=(deletion,),
                    skips=(mismatch_reason.value,),
                    diagnostics=self._diagnostics(
                        mismatch_reason.value.rsplit(".", 1)[-1],
                        sequence=current_sequence,
                        **dict(resolution.details),
                    ),
                )

        if self._active is not None:
            decision = self._process_active(view, values, current_sequence)
            self._complete_bar(values, current_sequence)
            return decision

        resolution = self._reference_for(view, values)
        reference = resolution.reference
        if reference is None:
            reason = resolution.reason or BreakoutReturnReason.INSUFFICIENT_REFERENCE
            self._complete_bar(values, current_sequence)
            return ModuleDecision(
                skips=(reason.value,),
                diagnostics=self._diagnostics(
                    reason.value.rsplit(".", 1)[-1],
                    history_count=self._history_count,
                    required=self.config.lookback_bars,
                    reference_mode=self.config.reference_mode.value,
                    **dict(resolution.details),
                ),
            )

        direction = self._detect_breakout(values, reference)
        if direction is None:
            self._complete_bar(values, current_sequence)
            return ModuleDecision(
                diagnostics=self._diagnostics(
                    "scanning",
                    range_high=float(reference.high),
                    range_low=float(reference.low),
                )
            )

        active = self._arm(direction, reference, values, current_sequence)
        self._active = active
        reason = (
            BreakoutReturnReason.BREAKOUT_ABOVE
            if direction is BreakoutDirection.ABOVE
            else BreakoutReturnReason.BREAKOUT_BELOW
        )
        if direction is BreakoutDirection.ABOVE:
            self._counters.armed_above += 1
        else:
            self._counters.armed_below += 1
        setup = SetupEvent(
            module=self.name,
            setup_id=active.setup_id,
            status=SetupStatus.ARMED,
            reason=reason.value,
            metadata=self._active_payload(active),
            symbol=values.symbol,
            side=active.signal_side,
        )
        feature = self._active_feature(active, values.close_time)
        self._complete_bar(values, current_sequence)
        self._log_transition("armed", active, reason=reason.value)
        return ModuleDecision(
            setups=(setup,),
            features=(feature,),
            diagnostics=self._diagnostics(
                "breakout_armed",
                **self._active_payload(active),
            ),
        )

    def _process_active(
        self,
        view: MarketView,
        values: _BarValues,
        current_sequence: int,
    ) -> ModuleDecision:
        active = self._require_active()
        bars_since_breakout = current_sequence - active.breakout_sequence

        if bars_since_breakout > self.config.max_return_bars:
            terminal = self._setup_terminal(
                SetupStatus.EXPIRED,
                BreakoutReturnReason.RETURN_EXPIRED,
            )
            deletion = self._active_delete("return_expired")
            self._counters.expired += 1
            self._log_transition(
                "expired",
                active,
                reason=BreakoutReturnReason.RETURN_EXPIRED.value,
            )
            self._active = None
            return ModuleDecision(
                setups=(terminal,),
                feature_deletes=(deletion,),
                skips=(BreakoutReturnReason.RETURN_EXPIRED.value,),
                diagnostics=self._diagnostics(
                    "return_expired",
                    bars_since_breakout=bars_since_breakout,
                ),
            )

        old_extreme = active.extreme
        if active.direction is BreakoutDirection.ABOVE:
            active.extreme = max(active.extreme, values.high)
            if values.close >= active.reference.high:
                return self._waiting_decision(
                    view,
                    values,
                    active,
                    old_extreme,
                    bars_since_breakout,
                )
            depth = (active.reference.high - values.close) / active.reference.width
        else:
            active.extreme = min(active.extreme, values.low)
            if values.close <= active.reference.low:
                return self._waiting_decision(
                    view,
                    values,
                    active,
                    old_extreme,
                    bars_since_breakout,
                )
            depth = (values.close - active.reference.low) / active.reference.width

        if depth > self._max_return_depth:
            terminal = self._setup_terminal(
                SetupStatus.REJECTED,
                BreakoutReturnReason.RETURN_TOO_DEEP,
                metadata={"return_depth": float(depth)},
            )
            deletion = self._active_delete("return_too_deep")
            self._counters.rejected += 1
            self._log_transition(
                "rejected",
                active,
                reason=BreakoutReturnReason.RETURN_TOO_DEEP.value,
            )
            self._active = None
            return ModuleDecision(
                setups=(terminal,),
                feature_deletes=(deletion,),
                skips=(BreakoutReturnReason.RETURN_TOO_DEEP.value,),
                diagnostics=self._diagnostics(
                    "return_too_deep",
                    return_depth=float(depth),
                    bars_since_breakout=bars_since_breakout,
                ),
            )

        if depth < self._min_return_depth or not self._return_is_confirmed(
            values, active.reference
        ):
            return self._waiting_decision(
                view,
                values,
                active,
                old_extreme,
                bars_since_breakout,
                depth=depth,
            )

        payload = {
            **self._active_payload(active),
            "confirmation_time": values.close_time.isoformat(),
            "return_open": float(values.open),
            "return_close": float(values.close),
            "return_depth": float(depth),
            "bars_to_return": bars_since_breakout,
        }
        setup = SetupEvent(
            module=self.name,
            setup_id=active.setup_id,
            status=SetupStatus.CONFIRMED,
            reason=BreakoutReturnReason.RETURN_CONFIRMED.value,
            metadata=payload,
            symbol=values.symbol,
            side=active.signal_side,
        )
        signal = Signal(
            module=self.name,
            side=active.signal_side,
            reason=BreakoutReturnReason.RETURN_CONFIRMED.value,
            metadata=payload,
        )
        confirmed = Feature(
            key=self.CONFIRMED_FEATURE,
            source_module=self.name,
            created_at=values.close_time,
            value=payload,
            symbol=values.symbol,
            timeframe=values.timeframe,
            valid_until=values.close_time,
            feature_id=f"{active.setup_id}:confirmed",
            invalidate_on_gap=True,
            metadata={"event": "confirmation"},
        )
        deletion = self._active_delete("return_confirmed")
        self._counters.confirmed += 1
        self._log_transition(
            "confirmed",
            active,
            reason=BreakoutReturnReason.RETURN_CONFIRMED.value,
        )
        self._active = None
        return ModuleDecision(
            setups=(setup,),
            signals=(signal,),
            features=(confirmed,),
            feature_deletes=(deletion,),
            diagnostics=self._diagnostics("return_confirmed", **payload),
        )

    def _waiting_decision(
        self,
        view: MarketView,
        values: _BarValues,
        active: ActiveBreakout,
        old_extreme: Decimal,
        bars_since_breakout: int,
        *,
        depth: Decimal | None = None,
    ) -> ModuleDecision:
        features, deletions = self._refresh_active_feature(
            view,
            active,
            old_extreme=old_extreme,
            created_at=values.close_time,
        )
        diagnostics: dict[str, object] = {
            "setup_id": active.setup_id,
            "bars_since_breakout": bars_since_breakout,
            "breakout_extreme": float(active.extreme),
        }
        if depth is not None:
            diagnostics["return_depth"] = float(depth)
        return ModuleDecision(
            features=features,
            feature_deletes=deletions,
            diagnostics=self._diagnostics("waiting_return", **diagnostics),
        )

    def _refresh_active_feature(
        self,
        view: MarketView,
        active: ActiveBreakout,
        *,
        old_extreme: Decimal,
        created_at: datetime,
    ) -> tuple[tuple[Feature, ...], tuple[FeatureDelete, ...]]:
        old_feature_id = self._active_feature_id(active)
        visible = self._feature_id_is_visible(view, old_feature_id, active)
        if active.extreme != old_extreme:
            deletion = (
                (self._feature_delete(active, old_feature_id, "extreme_updated"),)
                if visible
                else ()
            )
            active.feature_revision += 1
            return (self._active_feature(active, created_at),), deletion
        if not visible:
            self._counters.active_feature_republishes += 1
            return (self._active_feature(active, created_at),), ()
        return (), ()

    def _feature_id_is_visible(
        self,
        view: MarketView,
        feature_id: str,
        active: ActiveBreakout,
    ) -> bool:
        if view.features is None:
            return False
        return any(
            feature.feature_id == feature_id
            for feature in view.features.get_all(
                self.ACTIVE_FEATURE,
                symbol=active.symbol,
                timeframe=active.timeframe,
                source_module=self.name,
            )
        )

    def _reference_for(
        self,
        view: MarketView,
        values: _BarValues,
    ) -> _ReferenceResolution:
        if self.config.reference_mode is ReferenceMode.FEATURE:
            return self._feature_reference(view, values)
        if self._history_count < self.config.lookback_bars:
            return _ReferenceResolution(
                reference=None,
                reason=BreakoutReturnReason.INSUFFICIENT_REFERENCE,
            )
        if not self._maxima or not self._minima or self._last_bar_time is None:
            raise BreakoutReturnStateError("rolling window is inconsistent")
        high = self._maxima[0][1]
        low = self._minima[0][1]
        if high <= low:
            return _ReferenceResolution(
                reference=None,
                reason=BreakoutReturnReason.INSUFFICIENT_REFERENCE,
                details=MappingProxyType({"invalid_geometry": True}),
            )
        identifier = f"rolling:{self.config.lookback_bars}:{self._last_bar_time.isoformat()}"
        return _ReferenceResolution(
            ReferenceRange(high, low, identifier, "rolling", self._last_bar_time)
        )

    def _feature_reference(
        self,
        view: MarketView,
        values: _BarValues,
    ) -> _ReferenceResolution:
        if view.features is None:
            return _ReferenceResolution(None, BreakoutReturnReason.REFERENCE_MISSING)
        feature = view.features.latest(
            self.config.reference_feature_key,
            symbol=values.symbol,
            timeframe=self.config.reference_feature_timeframe,
        )
        if feature is None:
            return _ReferenceResolution(None, BreakoutReturnReason.REFERENCE_MISSING)

        expected_schema = self.config.reference_feature_schema_version
        if expected_schema is not None and feature.schema_version != expected_schema:
            return _ReferenceResolution(
                None,
                BreakoutReturnReason.REFERENCE_SCHEMA_MISMATCH,
                MappingProxyType(
                    {
                        "expected_schema_version": expected_schema,
                        "actual_schema_version": feature.schema_version,
                    }
                ),
            )

        max_age_seconds = self.config.max_reference_age_seconds
        if max_age_seconds is not None:
            age = values.close_time - feature.created_at
            if age > timedelta(seconds=max_age_seconds):
                return _ReferenceResolution(
                    None,
                    BreakoutReturnReason.REFERENCE_STALE,
                    MappingProxyType(
                        {
                            "reference_age_seconds": age.total_seconds(),
                            "max_reference_age_seconds": max_age_seconds,
                        }
                    ),
                )

        value = feature.value
        if not isinstance(value, Mapping):
            raise BreakoutReturnDataError(
                f"{self.config.reference_feature_key} must contain a mapping"
            )
        typed_value = cast(Mapping[str, object], value)
        if "high" not in typed_value or "low" not in typed_value:
            raise BreakoutReturnDataError(
                f"{self.config.reference_feature_key} requires high and low"
            )
        try:
            high = self._decimal(typed_value["high"], "reference.high")
            low = self._decimal(typed_value["low"], "reference.low")
            range_id = typed_value.get("range_id")
            identifier = (
                self._optional_identifier(range_id)
                or feature.feature_id
                or f"{feature.source_module}:{feature.created_at.isoformat()}"
            )
            return _ReferenceResolution(
                ReferenceRange(
                    high=high,
                    low=low,
                    identifier=identifier,
                    source=f"feature:{feature.key}:{feature.source_module}",
                    created_at=feature.created_at,
                ),
                details=MappingProxyType(
                    {
                        "reference_schema_version": feature.schema_version,
                        "reference_feature_id": feature.feature_id,
                    }
                ),
            )
        except (TypeError, ValueError) as exc:
            raise BreakoutReturnDataError(f"invalid reference range feature: {exc}") from exc

    def _reference_mismatch_reason(
        self,
        resolution: _ReferenceResolution,
    ) -> BreakoutReturnReason | None:
        active = self._require_active()
        current = resolution.reference
        if current is None:
            return resolution.reason or BreakoutReturnReason.REFERENCE_MISSING
        if (
            current.identifier != active.reference.identifier
            or current.high != active.reference.high
            or current.low != active.reference.low
            or current.source != active.reference.source
        ):
            return BreakoutReturnReason.REFERENCE_CHANGED
        return None

    def _detect_breakout(
        self,
        values: _BarValues,
        reference: ReferenceRange,
    ) -> BreakoutDirection | None:
        upper = reference.high * self._upper_breakout_factor
        lower = reference.low * self._lower_breakout_factor

        above = self._allow_above and values.close > upper
        below = self._allow_below and values.close < lower
        if self._breakout_requires_body:
            above = above and values.open > upper
            below = below and values.open < lower
        if above and below:
            raise BreakoutReturnDataError("a bar cannot break above and below simultaneously")
        if above:
            return BreakoutDirection.ABOVE
        if below:
            return BreakoutDirection.BELOW
        return None

    def _return_is_confirmed(
        self,
        values: _BarValues,
        reference: ReferenceRange,
    ) -> bool:
        if not self._return_requires_body:
            return True
        return (
            reference.low <= values.open <= reference.high
            and reference.low <= values.close <= reference.high
        )

    def _arm(
        self,
        direction: BreakoutDirection,
        reference: ReferenceRange,
        values: _BarValues,
        current_sequence: int,
    ) -> ActiveBreakout:
        side = Side.SELL if direction is BreakoutDirection.ABOVE else Side.BUY
        setup_id = self._setup_id(
            values.symbol,
            values.timeframe,
            direction,
            values.close_time,
        )
        extreme = values.high if direction is BreakoutDirection.ABOVE else values.low
        return ActiveBreakout(
            setup_id=setup_id,
            direction=direction,
            signal_side=side,
            reference=reference,
            breakout_time=values.close_time,
            breakout_sequence=current_sequence,
            extreme=extreme,
            symbol=values.symbol,
            timeframe=values.timeframe,
        )

    def _active_payload(self, active: ActiveBreakout) -> dict[str, object]:
        return {
            "setup_id": active.setup_id,
            "breakout_direction": active.direction.value,
            "signal_side": active.signal_side.value,
            "range_high": float(active.reference.high),
            "range_low": float(active.reference.low),
            "range_width": float(active.reference.width),
            "reference_id": active.reference.identifier,
            "reference_source": active.reference.source,
            "reference_created_at": active.reference.created_at.isoformat(),
            "breakout_time": active.breakout_time.isoformat(),
            "breakout_extreme": float(active.extreme),
            "symbol": active.symbol,
            "timeframe": active.timeframe,
            "feature_revision": active.feature_revision,
        }

    @staticmethod
    def _active_feature_id(active: ActiveBreakout) -> str:
        return f"{active.setup_id}:active:{active.feature_revision}"

    def _active_feature(self, active: ActiveBreakout, created_at: datetime) -> Feature:
        return Feature(
            key=self.ACTIVE_FEATURE,
            source_module=self.name,
            created_at=created_at,
            value=self._active_payload(active),
            symbol=active.symbol,
            timeframe=active.timeframe,
            feature_id=self._active_feature_id(active),
            invalidate_on_gap=True,
            metadata={"phase": BreakoutReturnPhase.WAITING_RETURN.value},
        )

    def _active_delete(self, reason: str) -> FeatureDelete:
        active = self._require_active()
        return self._feature_delete(
            active,
            self._active_feature_id(active),
            reason,
        )

    def _feature_delete(
        self,
        active: ActiveBreakout,
        feature_id: str,
        reason: str,
    ) -> FeatureDelete:
        return FeatureDelete(
            key=self.ACTIVE_FEATURE,
            source_module=self.name,
            feature_id=feature_id,
            symbol=active.symbol,
            timeframe=active.timeframe,
            reason=reason,
        )

    def _setup_terminal(
        self,
        status: SetupStatus,
        reason: BreakoutReturnReason,
        *,
        metadata: Mapping[str, object] | None = None,
    ) -> SetupEvent:
        active = self._require_active()
        payload = self._active_payload(active)
        if metadata:
            payload.update(metadata)
        return SetupEvent(
            module=self.name,
            setup_id=active.setup_id,
            status=status,
            reason=reason.value,
            metadata=payload,
            symbol=active.symbol,
            side=active.signal_side,
        )

    def _complete_bar(self, values: _BarValues, current_sequence: int) -> None:
        if self.config.reference_mode is ReferenceMode.ROLLING:
            while self._maxima and self._maxima[-1][1] <= values.high:
                self._maxima.pop()
            self._maxima.append((current_sequence, values.high))
            while self._minima and self._minima[-1][1] >= values.low:
                self._minima.pop()
            self._minima.append((current_sequence, values.low))
            stale_sequence = current_sequence - self.config.lookback_bars
            while self._maxima and self._maxima[0][0] <= stale_sequence:
                self._maxima.popleft()
            while self._minima and self._minima[0][0] <= stale_sequence:
                self._minima.popleft()
            self._history_count = min(self.config.lookback_bars, self._history_count + 1)

        self._sequence = current_sequence
        self._last_bar_time = values.timestamp
        self._last_bar_signature = values.signature

    def _reset_rolling_window(self) -> None:
        self._history_count = 0
        self._maxima.clear()
        self._minima.clear()

    def _validate_stream(self, values: _BarValues) -> bool:
        if self._bound_symbol is None:
            self._bound_symbol = values.symbol
            self._bound_timeframe = values.timeframe
        elif values.symbol != self._bound_symbol or values.timeframe != self._bound_timeframe:
            raise BreakoutReturnDataError(
                "one BreakoutReturnModule instance cannot switch symbol or timeframe"
            )

        if self._last_bar_time is None:
            return False
        if values.timestamp == self._last_bar_time:
            if values.signature == self._last_bar_signature:
                return True
            raise BreakoutReturnDataError(
                f"contradictory duplicate bar at {values.timestamp.isoformat()}"
            )
        if values.timestamp < self._last_bar_time:
            raise BreakoutReturnDataError(
                f"out-of-order bar {values.timestamp.isoformat()} "
                f"after {self._last_bar_time.isoformat()}"
            )
        return False

    def _bar_values(self, bar: Candle) -> _BarValues:
        timestamp = self._aware_utc(bar.timestamp, "bar.timestamp")
        symbol = self._string(bar.symbol, "bar.symbol")
        timeframe = self._string(bar.timeframe, "bar.timeframe")
        open_price = self._decimal(bar.open, "bar.open")
        high = self._decimal(bar.high, "bar.high")
        low = self._decimal(bar.low, "bar.low")
        close = self._decimal(bar.close, "bar.close")
        volume = self._decimal(bar.volume, "bar.volume", positive=False)
        self._validate_ohlc(open_price, high, low, close)
        signature = self._signature(
            timestamp,
            open_price,
            high,
            low,
            close,
            volume,
            symbol,
            timeframe,
        )
        return _BarValues(
            timestamp=timestamp,
            close_time=timestamp + self._close_delta(timeframe),
            symbol=symbol,
            timeframe=timeframe,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            signature=signature,
        )

    @staticmethod
    def _signature(
        timestamp: datetime,
        open_price: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
        volume: Decimal,
        symbol: str,
        timeframe: str,
    ) -> _BarSignature:
        return (timestamp, open_price, high, low, close, volume, symbol, timeframe)

    @staticmethod
    def _canonical_decimal(value: Decimal) -> str:
        if value == 0:
            return "0"
        return str(value.normalize())

    @staticmethod
    def _validate_ohlc(
        open_price: Decimal,
        high: Decimal,
        low: Decimal,
        close: Decimal,
    ) -> None:
        if high < max(open_price, close, low):
            raise ValueError("high is inconsistent with OHLC")
        if low > min(open_price, close, high):
            raise ValueError("low is inconsistent with OHLC")

    @staticmethod
    def _integer(value: object, name: str) -> int:
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise ValueError(f"{name} must be an integer")
        try:
            return int(value)
        except ValueError as exc:
            raise ValueError(f"{name} must be an integer") from exc

    @staticmethod
    def _string(value: object, name: str) -> str:
        if not isinstance(value, str) or not value.strip() or value != value.strip():
            raise ValueError(f"{name} must be a nonblank trimmed string")
        return value

    @staticmethod
    def _optional_identifier(value: object) -> str | None:
        if value is None:
            return None
        if not isinstance(value, str) or not value.strip():
            raise ValueError("reference.range_id must be a nonblank string when provided")
        return value

    @staticmethod
    def _decimal(value: object, name: str, *, positive: bool = True) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"{name} must be numeric")
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"{name} must be numeric") from exc
        if not result.is_finite() or (positive and result <= 0) or (not positive and result < 0):
            comparator = "positive" if positive else "non-negative"
            raise ValueError(f"{name} must be finite and {comparator}")
        return result

    @staticmethod
    def _aware_utc(value: datetime, name: str) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} must be timezone-aware")
        return value.astimezone(timezone.utc)

    def _close_delta(self, timeframe: str) -> timedelta:
        if timeframe == self._cached_timeframe:
            return self._cached_close_delta
        match = _TIMEFRAME_RE.match(timeframe.lower())
        if not match:
            delta = timedelta(0)
        else:
            amount = int(match.group(1))
            seconds = (
                amount
                * {
                    "s": 1,
                    "m": 60,
                    "h": 3600,
                    "d": 86_400,
                    "w": 604_800,
                }[match.group(2)]
            )
            delta = timedelta(seconds=seconds)
        self._cached_timeframe = timeframe
        self._cached_close_delta = delta
        return delta

    @classmethod
    def _serialize_signature(cls, signature: _BarSignature) -> list[str]:
        timestamp, open_price, high, low, close, volume, symbol, timeframe = signature
        return [
            timestamp.isoformat(),
            cls._canonical_decimal(open_price),
            cls._canonical_decimal(high),
            cls._canonical_decimal(low),
            cls._canonical_decimal(close),
            cls._canonical_decimal(volume),
            symbol,
            timeframe,
        ]

    @staticmethod
    def _parse_close_delta(timeframe: str) -> timedelta:
        match = _TIMEFRAME_RE.match(timeframe.lower())
        if not match:
            return timedelta(0)
        amount = int(match.group(1))
        seconds = (
            amount
            * {
                "s": 1,
                "m": 60,
                "h": 3600,
                "d": 86_400,
                "w": 604_800,
            }[match.group(2)]
        )
        return timedelta(seconds=seconds)

    @staticmethod
    def _setup_id(
        symbol: str,
        timeframe: str,
        direction: BreakoutDirection,
        close_time: datetime,
    ) -> str:
        safe_symbol = _SAFE_ID.sub("-", symbol).strip("-") or "symbol"
        safe_timeframe = _SAFE_ID.sub("-", timeframe).strip("-") or "timeframe"
        stamp = close_time.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        return f"br-{safe_symbol}-{safe_timeframe}-{direction.value}-{stamp}"

    def _require_active(self) -> ActiveBreakout:
        if self._active is None:
            raise BreakoutReturnStateError("active breakout is required")
        return self._active

    def _log_transition(self, event: str, active: ActiveBreakout, *, reason: str) -> None:
        LOGGER.info(
            "breakout_return_transition",
            extra={
                "module_name": self.name,
                "event": event,
                "reason": reason,
                "setup_id": active.setup_id,
                "symbol": active.symbol,
                "timeframe": active.timeframe,
                "direction": active.direction.value,
            },
        )

    def _diagnostics(self, event: str, **values: object) -> Mapping[str, object]:
        result: dict[str, object] = {
            "event": event,
            "phase": self.phase.value,
            "sequence": self._sequence,
        }
        result.update(values)
        if event not in {"scanning", "waiting_return"}:
            result["counters"] = dict(self._counters.as_mapping())
        return MappingProxyType(result)

    def _build_config_fingerprint(self) -> str:
        payload = dict(self.config.fingerprint_payload())
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def snapshot_state(self) -> Mapping[str, object]:
        active: dict[str, object] | None = None
        if self._active is not None:
            item = self._active
            active = {
                "setup_id": item.setup_id,
                "direction": item.direction.value,
                "signal_side": item.signal_side.value,
                "reference": {
                    "high": str(item.reference.high),
                    "low": str(item.reference.low),
                    "identifier": item.reference.identifier,
                    "source": item.reference.source,
                    "created_at": item.reference.created_at.isoformat(),
                },
                "breakout_time": item.breakout_time.isoformat(),
                "breakout_sequence": item.breakout_sequence,
                "extreme": str(item.extreme),
                "symbol": item.symbol,
                "timeframe": item.timeframe,
                "feature_revision": item.feature_revision,
            }
        return {
            "payload_version": _STATE_VERSION,
            "config_fingerprint": self._config_fingerprint,
            "sequence": self._sequence,
            "history_count": self._history_count,
            "maxima": [[sequence, str(value)] for sequence, value in self._maxima],
            "minima": [[sequence, str(value)] for sequence, value in self._minima],
            "active": active,
            "last_bar_time": self._last_bar_time.isoformat() if self._last_bar_time else None,
            "last_bar_signature": (
                self._serialize_signature(self._last_bar_signature)
                if self._last_bar_signature
                else None
            ),
            "bound_symbol": self._bound_symbol,
            "bound_timeframe": self._bound_timeframe,
            "counters": dict(self._counters.as_mapping()),
        }

    def restore_state(self, state: Mapping[str, object]) -> None:
        try:
            version = self._integer(state.get("payload_version", 0), "payload_version")
            if version not in {_LEGACY_STATE_VERSION, _STATE_VERSION}:
                raise BreakoutReturnStateError(
                    f"unsupported payload_version {version}; "
                    f"expected {_LEGACY_STATE_VERSION} or {_STATE_VERSION}"
                )
            self._validate_exact_keys(
                state,
                _STATE_KEYS_V1 if version == _LEGACY_STATE_VERSION else _STATE_KEYS_V2,
                "state",
            )
            if version == _STATE_VERSION:
                fingerprint = self._string(
                    state["config_fingerprint"],
                    "config_fingerprint",
                )
                if fingerprint != self._config_fingerprint:
                    raise BreakoutReturnStateError(
                        "checkpoint configuration fingerprint does not match current module"
                    )
                counters_raw = state["counters"]
                if not isinstance(counters_raw, Mapping):
                    raise BreakoutReturnStateError("counters must be a mapping")
                counters = BreakoutReturnCounters.from_mapping(
                    cast(Mapping[str, object], counters_raw)
                )
            else:
                counters = BreakoutReturnCounters()

            sequence = self._integer(state["sequence"], "sequence")
            history_count = self._integer(state["history_count"], "history_count")
            if sequence < 0 or not 0 <= history_count <= self.config.lookback_bars:
                raise BreakoutReturnStateError("invalid sequence/history_count")
            maxima = self._restore_extrema(state.get("maxima"), "maxima", decreasing=True)
            minima = self._restore_extrema(state.get("minima"), "minima", decreasing=False)
            last_bar_time = self._parse_optional_datetime(state.get("last_bar_time"))
            signature = self._restore_signature(state.get("last_bar_signature"))
            active_raw = state.get("active")
            active = self._restore_active(active_raw) if active_raw is not None else None

            if self.config.reference_mode is ReferenceMode.ROLLING:
                self._validate_rolling_state(
                    sequence,
                    history_count,
                    maxima,
                    minima,
                    signature,
                )
            elif history_count or maxima or minima:
                raise BreakoutReturnStateError("feature mode cannot restore rolling extrema")
            if (last_bar_time is None) != (signature is None):
                raise BreakoutReturnStateError("last bar timestamp/signature must appear together")
            if sequence == 0 and last_bar_time is not None:
                raise BreakoutReturnStateError("empty state cannot contain a last bar")
            if sequence > 0 and last_bar_time is None:
                raise BreakoutReturnStateError("processed state requires a last bar")
            if signature is not None and signature.timestamp != last_bar_time:
                raise BreakoutReturnStateError("last bar signature timestamp mismatch")

            bound_symbol_raw = state.get("bound_symbol")
            bound_timeframe_raw = state.get("bound_timeframe")
            if (bound_symbol_raw is None) != (bound_timeframe_raw is None):
                raise BreakoutReturnStateError("stream binding fields must appear together")
            bound_symbol = (
                self._string(bound_symbol_raw, "bound_symbol")
                if bound_symbol_raw is not None
                else None
            )
            bound_timeframe = (
                self._string(bound_timeframe_raw, "bound_timeframe")
                if bound_timeframe_raw is not None
                else None
            )
            if signature is not None and bound_symbol is not None:
                if signature.symbol != bound_symbol or signature.timeframe != bound_timeframe:
                    raise BreakoutReturnStateError("last bar signature contradicts stream binding")
            if active is not None:
                if active.breakout_sequence > sequence:
                    raise BreakoutReturnStateError(
                        "active breakout sequence exceeds state sequence"
                    )
                if signature is None:
                    raise BreakoutReturnStateError("active setup requires a last bar")
                if active.breakout_time > signature.close_time:
                    raise BreakoutReturnStateError(
                        "active breakout time exceeds the last processed bar close"
                    )
                if active.symbol != bound_symbol or active.timeframe != bound_timeframe:
                    raise BreakoutReturnStateError("active setup does not match stream binding")
                expected_setup_id = self._setup_id(
                    active.symbol,
                    active.timeframe,
                    active.direction,
                    active.breakout_time,
                )
                if active.setup_id != expected_setup_id:
                    raise BreakoutReturnStateError(
                        "active setup_id is inconsistent with deterministic identity fields"
                    )

            if version == _STATE_VERSION:
                if counters.bars_processed != sequence:
                    raise BreakoutReturnStateError(
                        "counters.bars_processed must equal state sequence"
                    )
                if counters.events_seen != counters.bars_processed + counters.duplicate_events:
                    raise BreakoutReturnStateError(
                        "counters.events_seen must equal processed bars plus duplicates"
                    )
            else:
                counters.events_seen = sequence
                counters.bars_processed = sequence
                counters.legacy_state_migrations = 1
        except BreakoutReturnStateError:
            raise
        except (KeyError, TypeError, ValueError) as exc:
            raise BreakoutReturnStateError(f"invalid breakout_return state: {exc}") from exc

        counters.restores += 1
        self._sequence = sequence
        self._history_count = history_count
        self._maxima = deque(maxima)
        self._minima = deque(minima)
        self._active = active
        self._last_bar_time = last_bar_time
        self._last_bar_signature = signature.signature if signature is not None else None
        self._bound_symbol = bound_symbol
        self._bound_timeframe = bound_timeframe
        self._cached_timeframe = bound_timeframe
        self._cached_close_delta = (
            self._parse_close_delta(bound_timeframe)
            if bound_timeframe is not None
            else timedelta(0)
        )
        self._counters = counters
        self._restored = True

    @classmethod
    def _validate_exact_keys(
        cls,
        raw: Mapping[str, object],
        expected: frozenset[str],
        name: str,
    ) -> None:
        unknown = sorted(set(raw) - expected)
        missing = sorted(expected - set(raw))
        if unknown or missing:
            raise BreakoutReturnStateError(
                f"invalid {name} keys; missing={missing}, unknown={unknown}"
            )

    def _validate_rolling_state(
        self,
        sequence: int,
        history_count: int,
        maxima: list[tuple[int, Decimal]],
        minima: list[tuple[int, Decimal]],
        signature: _BarValues | None,
    ) -> None:
        if history_count and (not maxima or not minima):
            raise BreakoutReturnStateError("non-empty rolling history requires extrema")
        if len(maxima) > history_count or len(minima) > history_count:
            raise BreakoutReturnStateError("extrema cannot exceed rolling history")
        lower_bound = sequence - history_count
        if any(
            item_sequence <= lower_bound or item_sequence > sequence
            for item_sequence, _ in (*maxima, *minima)
        ):
            raise BreakoutReturnStateError(
                "extrema sequence is outside the restored rolling-history horizon"
            )
        if history_count:
            if signature is None:
                raise BreakoutReturnStateError("rolling history requires a last bar signature")
            if maxima[-1][0] != sequence or minima[-1][0] != sequence:
                raise BreakoutReturnStateError("extrema must include the current sequence")
            if maxima[-1][1] != signature.high or minima[-1][1] != signature.low:
                raise BreakoutReturnStateError(
                    "current extrema entries must match the last bar high/low"
                )

    def _restore_extrema(
        self,
        raw: object,
        name: str,
        *,
        decreasing: bool,
    ) -> list[tuple[int, Decimal]]:
        if raw is None:
            return []
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence):
            raise BreakoutReturnStateError(f"{name} must be a sequence")
        result: list[tuple[int, Decimal]] = []
        previous_sequence = -1
        previous_value: Decimal | None = None
        for row in raw:
            if isinstance(row, (str, bytes)) or not isinstance(row, Sequence) or len(row) != 2:
                raise BreakoutReturnStateError(f"invalid {name} entry")
            item_sequence = self._integer(row[0], f"{name}.sequence")
            value = self._decimal(row[1], name)
            if item_sequence <= previous_sequence:
                raise BreakoutReturnStateError(f"{name} sequences must increase")
            if previous_value is not None:
                if decreasing and previous_value <= value:
                    raise BreakoutReturnStateError("maxima values must strictly decrease")
                if not decreasing and previous_value >= value:
                    raise BreakoutReturnStateError("minima values must strictly increase")
            result.append((item_sequence, value))
            previous_sequence = item_sequence
            previous_value = value
        return result

    def _restore_signature(self, raw: object) -> _BarValues | None:
        if raw is None:
            return None
        if isinstance(raw, (str, bytes)) or not isinstance(raw, Sequence) or len(raw) != 8:
            raise BreakoutReturnStateError("last_bar_signature must contain eight values")
        timestamp = self._parse_datetime(raw[0])
        open_price = self._decimal(raw[1], "last_bar_signature.open")
        high = self._decimal(raw[2], "last_bar_signature.high")
        low = self._decimal(raw[3], "last_bar_signature.low")
        close = self._decimal(raw[4], "last_bar_signature.close")
        volume = self._decimal(raw[5], "last_bar_signature.volume", positive=False)
        symbol = self._string(raw[6], "last_bar_signature.symbol")
        timeframe = self._string(raw[7], "last_bar_signature.timeframe")
        self._validate_ohlc(open_price, high, low, close)
        signature = self._signature(
            timestamp,
            open_price,
            high,
            low,
            close,
            volume,
            symbol,
            timeframe,
        )
        return _BarValues(
            timestamp=timestamp,
            close_time=timestamp + self._parse_close_delta(timeframe),
            symbol=symbol,
            timeframe=timeframe,
            open=open_price,
            high=high,
            low=low,
            close=close,
            volume=volume,
            signature=signature,
        )

    def _restore_active(self, raw: object) -> ActiveBreakout:
        if not isinstance(raw, Mapping):
            raise BreakoutReturnStateError("active must be a mapping")
        active_raw = cast(Mapping[str, object], raw)
        self._validate_exact_keys(active_raw, _ACTIVE_KEYS, "active")
        reference_raw_object = active_raw["reference"]
        if not isinstance(reference_raw_object, Mapping):
            raise BreakoutReturnStateError("active.reference must be a mapping")
        reference_raw = cast(Mapping[str, object], reference_raw_object)
        self._validate_exact_keys(reference_raw, _REFERENCE_KEYS, "active.reference")
        reference = ReferenceRange(
            high=self._decimal(reference_raw["high"], "active.reference.high"),
            low=self._decimal(reference_raw["low"], "active.reference.low"),
            identifier=self._string(
                reference_raw["identifier"],
                "active.reference.identifier",
            ),
            source=self._string(reference_raw["source"], "active.reference.source"),
            created_at=self._parse_datetime(reference_raw["created_at"]),
        )
        try:
            active = ActiveBreakout(
                setup_id=self._string(active_raw["setup_id"], "active.setup_id"),
                direction=BreakoutDirection(
                    self._string(active_raw["direction"], "active.direction")
                ),
                signal_side=Side(self._string(active_raw["signal_side"], "active.signal_side")),
                reference=reference,
                breakout_time=self._parse_datetime(active_raw["breakout_time"]),
                breakout_sequence=self._integer(
                    active_raw["breakout_sequence"],
                    "active.breakout_sequence",
                ),
                extreme=self._decimal(active_raw["extreme"], "active.extreme"),
                symbol=self._string(active_raw["symbol"], "active.symbol"),
                timeframe=self._string(active_raw["timeframe"], "active.timeframe"),
                feature_revision=self._integer(
                    active_raw["feature_revision"],
                    "active.feature_revision",
                ),
            )
        except ValueError as exc:
            raise BreakoutReturnStateError(f"invalid active setup: {exc}") from exc
        return active

    @classmethod
    def _parse_datetime(cls, raw: object) -> datetime:
        if not isinstance(raw, str):
            raise BreakoutReturnStateError("state datetime must be an ISO string")
        try:
            value = datetime.fromisoformat(raw)
        except ValueError as exc:
            raise BreakoutReturnStateError("state datetime is not valid ISO-8601") from exc
        return cls._aware_utc(value, "state datetime")

    @classmethod
    def _parse_optional_datetime(cls, raw: object) -> datetime | None:
        return None if raw is None else cls._parse_datetime(raw)
