from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import UTC
from decimal import Decimal, localcontext

from modular_core import (
    CancelRequest,
    Candle,
    ClosePositionRequest,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    PositionSide,
    PositionUpdateRequest,
)

from . import module as _module_namespace
from .config import _MAX_HOLDING_MINUTES
from .constants import MAX_COUNTER_VALUE
from .decimal_utils import DECIMAL_CONTEXT, exact_decimal_text, validate_decimal_bounds
from .models import ManagementAction, PositionExitReason, TrailingMode, TriggerPriceSource
from .module import (
    MANIFEST as _BASE_MANIFEST,
)
from .module import (
    PositionExitManagementModule as _BasePositionExitManagementModule,
)
from .module import (
    _decimal,
    _require_nonblank_input,
    _validate_view,
)
from .state import ModuleRuntimeState, PositionRuntimeState


def _validate_view_envelope(view: MarketView) -> None:
    if isinstance(view.base_bars, str | bytes) or not isinstance(
        view.base_bars,
        Sequence,
    ):
        raise TypeError("view.base_bars must be a candle sequence")
    if not view.base_bars:
        raise ValueError("view.base_bars cannot be empty")
    current = view.base_bars[-1]
    if not isinstance(current, Candle):
        raise TypeError("view.current must be Candle")
    _require_nonblank_input(current.symbol, "current.symbol")
    _require_nonblank_input(current.timeframe, "current.timeframe")
    try:
        _ = current.close_time
    except (OverflowError, ValueError) as exc:
        raise ValueError("current.close_time must be representable") from exc
    if view.index > MAX_COUNTER_VALUE:
        raise ValueError(f"view.index cannot exceed {MAX_COUNTER_VALUE}")


def _validate_position_transport_domain(view: MarketView) -> None:
    position = view.position
    if position is None:
        return
    values = (
        (position.quantity, "position.quantity"),
        (position.entry_price, "position.entry_price"),
        (position.stop_loss, "position.stop_loss"),
        (position.take_profit, "position.take_profit"),
    )
    for value, name in values:
        if value is None or isinstance(value, bool) or not isinstance(value, int | float | Decimal):
            continue
        try:
            encoded = float(value)
        except (OverflowError, ValueError) as exc:
            raise ValueError(f"{name} must fit a finite positive float") from exc
        if isinstance(value, Decimal):
            source_is_positive = value.is_finite() and value > 0
        else:
            try:
                source_is_positive = math.isfinite(value) and value > 0
            except OverflowError:
                source_is_positive = value > 0
        if source_is_positive and (not math.isfinite(encoded) or encoded <= 0):
            raise ValueError(f"{name} must fit a finite positive float")


def _safe_diagnostic_value(value: object) -> object:
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _safe_diagnostic_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return tuple(_safe_diagnostic_value(item) for item in value)
    if isinstance(value, list):
        return [_safe_diagnostic_value(item) for item in value]
    return value


def _hardened_manifest() -> ModuleManifest:
    schema = {name: dict(rules) for name, rules in _BASE_MANIFEST.parameters_schema.items()}
    schema["max_holding_minutes"]["maximum"] = _MAX_HOLDING_MINUTES
    for name in (
        "cancel_retry_bars",
        "max_tracked_cancels",
        "max_cancel_requests_per_bar",
    ):
        schema[name]["maximum"] = MAX_COUNTER_VALUE
    return replace(_BASE_MANIFEST, parameters_schema=schema)


MANIFEST = _hardened_manifest()


class PositionExitManagementModule(_BasePositionExitManagementModule):
    """Safety-hardened public implementation of the position manager.

    The base implementation remains the single algorithmic implementation. This
    class narrows lifecycle edges without changing the public Module API:

    * malformed views and future positions fail before stream/cursor mutation;
    * impossible R-dependent checkpoints are rejected transactionally;
    * restored position time cannot exceed the saved event cursor;
    * an entry bar cannot use OHLC extremes that may predate the actual fill;
    * confirmed partial stages remain observable without repeating an action;
    * public float DTOs and JSON diagnostics remain finite and representable;
    * Decimal arithmetic is isolated from the caller's process-wide context;
    * event and checkpoint counters remain bounded and serializable.
    """

    manifest = MANIFEST

    def on_bar(self, view: MarketView) -> ModuleDecision:
        with localcontext(DECIMAL_CONTEXT):
            return self._on_bar_deterministic(view)

    def _on_bar_deterministic(self, view: MarketView) -> ModuleDecision:
        _validate_view_envelope(view)
        _validate_position_transport_domain(view)
        _validate_view(view)
        close_time = view.current.close_time
        position = view.position
        state = self._state
        stream_matches = state.stream_symbol is None or (
            state.stream_symbol,
            state.stream_timeframe,
        ) == (view.current.symbol, view.current.timeframe)
        cursor_advances = state.last_close_time is None or close_time > state.last_close_time
        if (
            position is not None
            and stream_matches
            and cursor_advances
            and position.entry_time > close_time
        ):
            current = view.current
            return self._decision(
                view,
                reason=PositionExitReason.FUTURE_ENTRY_TIME,
                skips=(PositionExitReason.FUTURE_ENTRY_TIME.value,),
                diagnostics={
                    "position_id": position.id,
                    "symbol": current.symbol,
                    "timeframe": current.timeframe,
                    "entry_time": position.entry_time.astimezone(UTC).isoformat(),
                    "close_time": close_time.isoformat(),
                },
            )

        rejected = self._preflight_event(view, close_time)
        if rejected is not None:
            return rejected
        if position is None:
            return self._handle_flat(view)

        decision = self._handle_position(view, close_time)
        if "partial_completed_index" not in decision.diagnostics or self._has_primary_effect(
            decision
        ):
            return decision

        runtime = self._state.active_position
        current_r = None if runtime is None else self._current_r(runtime, view)
        return self._decision(
            view,
            reason=PositionExitReason.PARTIAL_EXIT_CONFIRMED,
            diagnostics=decision.diagnostics,
            current_r=current_r,
        )

    def restore_state(self, state: Mapping[str, object]) -> None:
        with localcontext(DECIMAL_CONTEXT):
            super().restore_state(state)

    @staticmethod
    def _has_primary_effect(decision: ModuleDecision) -> bool:
        return bool(
            decision.setups
            or decision.signals
            or decision.feature_deletes
            or decision.orders
            or decision.cancellations
            or decision.position_updates
            or decision.closes
            or decision.skips
        )

    def _decision(
        self,
        view: MarketView,
        *,
        reason: PositionExitReason,
        action: ManagementAction = ManagementAction.NONE,
        cancellations: tuple[CancelRequest, ...] = (),
        position_updates: tuple[PositionUpdateRequest, ...] = (),
        closes: tuple[ClosePositionRequest, ...] = (),
        skips: tuple[str, ...] = (),
        diagnostics: Mapping[str, object] | None = None,
        current_r: Decimal | None = None,
    ) -> ModuleDecision:
        details = {
            key: _safe_diagnostic_value(value) for key, value in dict(diagnostics or {}).items()
        }
        if current_r is not None:
            try:
                encoded_r = float(current_r)
            except (OverflowError, ValueError):
                encoded_r = math.inf
            if not math.isfinite(encoded_r):
                details["current_r"] = exact_decimal_text(current_r)
        return super()._decision(
            view,
            reason=reason,
            action=action,
            cancellations=cancellations,
            position_updates=position_updates,
            closes=closes,
            skips=skips,
            diagnostics=details,
            current_r=current_r,
        )

    def _validate_restored_state(self, state: ModuleRuntimeState) -> None:
        super()._validate_restored_state(state)
        for order_id, index in state.cancel_attempts.items():
            if index > MAX_COUNTER_VALUE:
                raise ValueError(
                    f"cancel attempt index for {order_id!r} cannot exceed {MAX_COUNTER_VALUE}"
                )
        runtime = state.active_position
        if runtime is None:
            return
        if runtime.bars_managed > MAX_COUNTER_VALUE:
            raise ValueError(f"bars_managed cannot exceed {MAX_COUNTER_VALUE}")
        decimal_fields = (
            (runtime.initial_quantity, "initial_quantity"),
            (runtime.last_seen_quantity, "last_seen_quantity"),
            (runtime.entry_price, "entry_price"),
            (runtime.initial_stop, "initial_stop"),
            (runtime.initial_risk_per_unit, "initial_risk_per_unit"),
            (runtime.last_stop, "last_stop"),
            (runtime.favorable_extreme, "favorable_extreme"),
            (runtime.pending_partial_target_quantity, "pending_partial_target_quantity"),
        )
        for value, name in decimal_fields:
            if value is not None:
                validate_decimal_bounds(value, f"active_position.{name}")
        if state.last_close_time is None or runtime.entry_time > state.last_close_time:
            raise ValueError("active position entry_time cannot exceed event cursor")

        has_partial_progress = bool(runtime.completed_partial_indices) or (
            runtime.pending_partial_index is not None
        )
        if has_partial_progress and runtime.initial_risk_per_unit is None:
            raise ValueError("partial-exit state requires initial risk")

        percent_trailing_requires_r = (
            self.config.trailing_mode is TrailingMode.PERCENT
            and self.config.trailing_activation_r > 0
        )
        if (
            runtime.trailing_active
            and percent_trailing_requires_r
            and runtime.initial_risk_per_unit is None
        ):
            raise ValueError("R-activated percent trailing state requires initial risk")

    def _update_favorable_extreme(
        self,
        runtime: PositionRuntimeState,
        view: MarketView,
    ) -> None:
        if runtime.entry_time <= view.current.timestamp:
            super()._update_favorable_extreme(runtime, view)
            return

        close = _decimal(view.current.close)
        if runtime.side is PositionSide.LONG:
            runtime.favorable_extreme = max(runtime.favorable_extreme, close)
        else:
            runtime.favorable_extreme = min(runtime.favorable_extreme, close)

    def _current_r(
        self,
        runtime: PositionRuntimeState,
        view: MarketView,
    ) -> Decimal | None:
        entry_bar_contains_pre_fill_data = runtime.entry_time > view.current.timestamp
        if (
            self.config.trigger_source is not TriggerPriceSource.EXTREME
            or not entry_bar_contains_pre_fill_data
        ):
            return super()._current_r(runtime, view)

        risk = runtime.initial_risk_per_unit
        if risk is None:
            return None
        close = _decimal(view.current.close)
        move = (
            close - runtime.entry_price
            if runtime.side is PositionSide.LONG
            else runtime.entry_price - close
        )
        return move / risk

    def _improves_stop(
        self,
        existing: Decimal | None,
        candidate: Decimal,
        side: PositionSide,
    ) -> bool:
        try:
            encoded = float(candidate)
        except (OverflowError, ValueError):
            return False
        if not math.isfinite(encoded) or encoded <= 0:
            return False
        return super()._improves_stop(existing, candidate, side)


# Preserve historical direct import paths with one public implementation and manifest.
setattr(_module_namespace, "MANIFEST", MANIFEST)  # noqa: B010
setattr(  # noqa: B010
    _module_namespace,
    "PositionExitManagementModule",
    PositionExitManagementModule,
)
