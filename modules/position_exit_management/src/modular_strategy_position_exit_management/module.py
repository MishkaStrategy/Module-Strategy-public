from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from heapq import nsmallest
from types import MappingProxyType
from typing import cast

from modular_core import (
    CancelRequest,
    ClosePositionRequest,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    PositionSide,
    PositionUpdateRequest,
    StrategyModule,
    Trade,
)

from .config import PositionExitConfig
from .constants import (
    FEATURE_SCHEMA_VERSION,
    MODULE_API_VERSION,
    MODULE_NAME,
    MODULE_VERSION,
    STATE_SCHEMA_VERSION,
    STATUS_FEATURE_KEY,
)
from .models import (
    ManagementAction,
    PositionExitReason,
    PositionManagementStatus,
    TrailingMode,
    TriggerPriceSource,
)
from .state import ModuleRuntimeState, PositionRuntimeState


def _float_transport_tolerance(*values: Decimal) -> Decimal:
    """Return a bounded tolerance matching Core's float transport resolution."""

    ulps = (Decimal(str(math.ulp(float(value)))) for value in values)
    return max(ulps, default=Decimal("0")) * Decimal("2")


@dataclass(frozen=True, slots=True)
class _PositionLifecycleRule:
    entry_index: int
    max_hold_bars: int | None
    no_progress_bars: int | None
    no_progress_min_mfe_r: Decimal | None


def _partial_transport_quantity(
    current_quantity: Decimal,
    requested_quantity: Decimal,
    target_quantity: Decimal,
) -> float | None:
    """Encode a partial close without collapsing a positive target to zero."""

    try:
        encoded_current = float(current_quantity)
        encoded_requested = float(requested_quantity)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(encoded_current) or encoded_current <= 0:
        return None
    if target_quantity > 0 and encoded_requested >= encoded_current:
        encoded_requested = math.nextafter(encoded_current, 0.0)
    if not math.isfinite(encoded_requested) or encoded_requested <= 0:
        return None
    return encoded_requested


MANIFEST = ModuleManifest(
    name=MODULE_NAME,
    version=MODULE_VERSION,
    api_version=MODULE_API_VERSION,
    required_timeframes=(),
    required_features=(),
    optional_features=(),
    produced_features=(STATUS_FEATURE_KEY,),
    warmup_bars={},
    parameters_schema={
        "max_holding_minutes": {"type": "integer", "minimum": 1},
        "partial_exits": {"type": "array", "default": ()},
        "breakeven_trigger_r": {"type": "string"},
        "breakeven_offset_r": {"type": "string", "default": "0"},
        "trailing_mode": {
            "type": "string",
            "enum": tuple(item.value for item in TrailingMode),
            "default": TrailingMode.NONE.value,
        },
        "trailing_activation_r": {"type": "string", "default": "1"},
        "trailing_distance_r": {
            "type": "string",
            "default": "1",
        },
        "trailing_distance_pct": {
            "type": "string",
            "default": "0.01",
        },
        "trigger_source": {
            "type": "string",
            "enum": tuple(item.value for item in TriggerPriceSource),
            "default": TriggerPriceSource.CLOSE.value,
        },
        "minimum_stop_move_bps": {
            "type": "string",
            "default": "1",
        },
        "market_buffer_bps": {
            "type": "string",
            "default": "2",
        },
        "cancel_reduce_only_orders_on_flat": {"type": "boolean", "default": True},
        "cancel_managed_tag_orders_on_flat": {"type": "boolean", "default": True},
        "managed_order_tag_prefix": {"type": "string", "default": "position-exit:"},
        "cancel_retry_bars": {"type": "integer", "minimum": 1, "default": 1},
        "max_tracked_cancels": {"type": "integer", "minimum": 1, "default": 1024},
        "max_cancel_requests_per_bar": {"type": "integer", "minimum": 1, "default": 64},
        "reset_on_scale_in": {"type": "boolean", "default": True},
        "honor_position_lifecycle_metadata": {"type": "boolean", "default": False},
        "suspend_price_actions_on_gap": {"type": "boolean", "default": True},
        "emit_idle_skips": {"type": "boolean", "default": False},
        "emit_status_feature": {"type": "boolean", "default": False},
    },
    skip_until_ready=False,
    description=(
        "Deterministic open-position manager with time exits, staged partial exits, "
        "breakeven, trailing protection and bounded orphan-order cleanup."
    ),
    state_schema_version=STATE_SCHEMA_VERSION,
)


class PositionExitManagementModule(StrategyModule):
    name = MODULE_NAME
    manifest = MANIFEST

    def __init__(
        self,
        *,
        max_holding_minutes: int | None = None,
        partial_exits: Sequence[object] = (),
        breakeven_trigger_r: int | float | str | Decimal | None = None,
        breakeven_offset_r: int | float | str | Decimal = 0,
        trailing_mode: str | TrailingMode = TrailingMode.NONE,
        trailing_activation_r: int | float | str | Decimal = 1,
        trailing_distance_r: int | float | str | Decimal = 1,
        trailing_distance_pct: int | float | str | Decimal = "0.01",
        trigger_source: str | TriggerPriceSource = TriggerPriceSource.CLOSE,
        minimum_stop_move_bps: int | float | str | Decimal = 1,
        market_buffer_bps: int | float | str | Decimal = 2,
        cancel_reduce_only_orders_on_flat: bool = True,
        cancel_managed_tag_orders_on_flat: bool = True,
        managed_order_tag_prefix: str = "position-exit:",
        cancel_retry_bars: int = 1,
        max_tracked_cancels: int = 1024,
        max_cancel_requests_per_bar: int = 64,
        reset_on_scale_in: bool = True,
        honor_position_lifecycle_metadata: bool = False,
        suspend_price_actions_on_gap: bool = True,
        emit_idle_skips: bool = False,
        emit_status_feature: bool = False,
    ) -> None:
        self.config = PositionExitConfig.from_parameters(
            max_holding_minutes=max_holding_minutes,
            partial_exits=partial_exits,
            breakeven_trigger_r=breakeven_trigger_r,
            breakeven_offset_r=breakeven_offset_r,
            trailing_mode=trailing_mode,
            trailing_activation_r=trailing_activation_r,
            trailing_distance_r=trailing_distance_r,
            trailing_distance_pct=trailing_distance_pct,
            trigger_source=trigger_source,
            minimum_stop_move_bps=minimum_stop_move_bps,
            market_buffer_bps=market_buffer_bps,
            cancel_reduce_only_orders_on_flat=cancel_reduce_only_orders_on_flat,
            cancel_managed_tag_orders_on_flat=cancel_managed_tag_orders_on_flat,
            managed_order_tag_prefix=managed_order_tag_prefix,
            cancel_retry_bars=cancel_retry_bars,
            max_tracked_cancels=max_tracked_cancels,
            max_cancel_requests_per_bar=max_cancel_requests_per_bar,
            reset_on_scale_in=reset_on_scale_in,
            honor_position_lifecycle_metadata=honor_position_lifecycle_metadata,
            suspend_price_actions_on_gap=suspend_price_actions_on_gap,
            emit_idle_skips=emit_idle_skips,
            emit_status_feature=emit_status_feature,
        )
        self.parameters = MappingProxyType(self._resolved_parameters())
        self._state = ModuleRuntimeState(config_fingerprint=self.config.fingerprint)
        self._price_actions_enabled = bool(
            self.config.partial_exits
            or self.config.breakeven_trigger_r is not None
            or self.config.trailing_mode is not TrailingMode.NONE
        )
        self._routine_fast_path_enabled = bool(
            not self._price_actions_enabled
            and self.config.max_holding_minutes is None
            and not self.config.honor_position_lifecycle_metadata
            and not self.config.emit_status_feature
            and not self.config.emit_idle_skips
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        _validate_view(view)
        close_time = view.current.close_time
        rejected = self._preflight_event(view, close_time)
        if rejected is not None:
            return rejected
        if view.position is None:
            return self._handle_flat(view)
        return self._handle_position(view, close_time)

    def on_trade(self, trade: Trade) -> None:
        if trade.position_id is not None:
            _require_nonblank_input(trade.position_id, "trade.position_id")
        if not isinstance(trade.position_closed, bool):
            raise TypeError("trade.position_closed must be boolean")
        state = self._state.active_position
        if state is None or trade.position_id != state.position_id:
            return
        if trade.position_closed:
            state.full_close_requested = False
            state.pending_partial_index = None
            state.pending_partial_target_quantity = None

    def snapshot_state(self) -> Mapping[str, object]:
        return self._state.to_mapping()

    def restore_state(self, state: Mapping[str, object]) -> None:
        if not state:
            self._state = ModuleRuntimeState(config_fingerprint=self.config.fingerprint)
            return
        restored = ModuleRuntimeState.from_mapping(
            state,
            expected_config_fingerprint=self.config.fingerprint,
            partial_rule_count=len(self.config.partial_exits),
            max_tracked_cancels=self.config.max_tracked_cancels,
        )
        self._validate_restored_state(restored)
        self._state = restored

    def _validate_restored_state(self, state: ModuleRuntimeState) -> None:
        runtime = state.active_position
        if runtime is None:
            return
        if runtime.full_close_requested and self.config.max_holding_minutes is None:
            raise ValueError("full-close state requires time-exit configuration")
        if runtime.breakeven_applied:
            if self.config.breakeven_trigger_r is None:
                raise ValueError("breakeven state requires breakeven configuration")
            if runtime.initial_risk_per_unit is None:
                raise ValueError("breakeven state requires initial risk")
        if runtime.trailing_active:
            if self.config.trailing_mode is TrailingMode.NONE:
                raise ValueError("trailing state requires trailing configuration")
            if (
                self.config.trailing_mode is TrailingMode.R_MULTIPLE
                and runtime.initial_risk_per_unit is None
            ):
                raise ValueError("R-multiple trailing state requires initial risk")

    def _resolved_parameters(self) -> dict[str, object]:
        canonical = self.config.canonical_mapping()
        partial_exits = cast(tuple[Mapping[str, str], ...], canonical["partial_exits"])
        result: dict[str, object] = {
            "partial_exits": tuple(MappingProxyType(dict(rule)) for rule in partial_exits),
            "breakeven_offset_r": canonical["breakeven_offset_r"],
            "trailing_mode": canonical["trailing_mode"],
            "trailing_activation_r": canonical["trailing_activation_r"],
            "trailing_distance_r": canonical["trailing_distance_r"],
            "trailing_distance_pct": canonical["trailing_distance_pct"],
            "trigger_source": canonical["trigger_source"],
            "minimum_stop_move_bps": canonical["minimum_stop_move_bps"],
            "market_buffer_bps": canonical["market_buffer_bps"],
            "cancel_reduce_only_orders_on_flat": canonical["cancel_reduce_only_orders_on_flat"],
            "cancel_managed_tag_orders_on_flat": canonical["cancel_managed_tag_orders_on_flat"],
            "managed_order_tag_prefix": canonical["managed_order_tag_prefix"],
            "cancel_retry_bars": canonical["cancel_retry_bars"],
            "max_tracked_cancels": canonical["max_tracked_cancels"],
            "max_cancel_requests_per_bar": canonical["max_cancel_requests_per_bar"],
            "reset_on_scale_in": canonical["reset_on_scale_in"],
            "suspend_price_actions_on_gap": canonical["suspend_price_actions_on_gap"],
            "emit_idle_skips": canonical["emit_idle_skips"],
            "emit_status_feature": canonical["emit_status_feature"],
        }
        if self.config.honor_position_lifecycle_metadata:
            result["honor_position_lifecycle_metadata"] = True
        if canonical["max_holding_minutes"] is not None:
            result["max_holding_minutes"] = canonical["max_holding_minutes"]
        if canonical["breakeven_trigger_r"] is not None:
            result["breakeven_trigger_r"] = canonical["breakeven_trigger_r"]
        return result

    def _preflight_event(
        self,
        view: MarketView,
        close_time: datetime,
    ) -> ModuleDecision | None:
        current = view.current
        state = self._state
        if state.stream_symbol is None:
            state.stream_symbol = current.symbol
            state.stream_timeframe = current.timeframe
        elif (state.stream_symbol, state.stream_timeframe) != (
            current.symbol,
            current.timeframe,
        ):
            reason = (
                PositionExitReason.STREAM_CHANGED_WITH_POSITION
                if view.position is not None or state.active_position is not None
                else PositionExitReason.STREAM_CHANGED
            )
            if reason is PositionExitReason.STREAM_CHANGED_WITH_POSITION:
                return ModuleDecision(
                    skips=(reason.value,),
                    diagnostics={
                        "expected_symbol": state.stream_symbol,
                        "expected_timeframe": state.stream_timeframe,
                        "actual_symbol": current.symbol,
                        "actual_timeframe": current.timeframe,
                    },
                )
            state.stream_symbol = current.symbol
            state.stream_timeframe = current.timeframe
            state.last_close_time = None
            state.last_event_fingerprint = None
            state.cancel_attempts.clear()

        fingerprint = _event_fingerprint(view)
        if state.last_close_time is not None:
            if close_time < state.last_close_time:
                return ModuleDecision(
                    skips=(PositionExitReason.OUT_OF_ORDER_EVENT.value,),
                    diagnostics={
                        "last_close_time": state.last_close_time.isoformat(),
                        "actual_close_time": close_time.isoformat(),
                    },
                )
            if close_time == state.last_close_time:
                reason = (
                    PositionExitReason.DUPLICATE_EVENT
                    if fingerprint == state.last_event_fingerprint
                    else PositionExitReason.CONFLICTING_DUPLICATE
                )
                return ModuleDecision(
                    skips=(reason.value,),
                    diagnostics={"event_fingerprint": fingerprint},
                )
        state.last_close_time = close_time
        state.last_event_fingerprint = fingerprint
        return None

    def _handle_position(
        self,
        view: MarketView,
        close_time: datetime,
    ) -> ModuleDecision:
        position = view.position
        if position is None:
            raise RuntimeError("position handler requires an open position")
        current = view.current
        if position.entry_time > close_time:
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
        runtime = self._state.active_position
        if (
            self._routine_fast_path_enabled
            and runtime is not None
            and runtime.position_id == position.id
            and runtime.side is position.side
            and runtime.entry_time == position.entry_time
            and runtime.setup_id == position.setup_id
        ):
            fast_decision = self._routine_monitoring_fast_path(view, runtime, close_time)
            if fast_decision is not None:
                return fast_decision

        current_close = _decimal(current.close)
        diagnostics: dict[str, object] = {
            "position_id": position.id,
            "symbol": current.symbol,
            "timeframe": current.timeframe,
        }
        reason = PositionExitReason.POSITION_MONITORED
        lifecycle = self._position_lifecycle_rule(view)
        runtime_initialized = False
        runtime = self._state.active_position
        if runtime is None or runtime.position_id != position.id:
            reason = (
                PositionExitReason.POSITION_INITIALIZED
                if runtime is None
                else PositionExitReason.POSITION_REPLACED
            )
            runtime = PositionRuntimeState.from_position(position, current_close)
            self._state.active_position = runtime
            runtime_initialized = True
        else:
            identity_changed = (
                runtime.side is not position.side
                or runtime.entry_time != position.entry_time
                or runtime.setup_id != position.setup_id
            )
            if identity_changed:
                reason = PositionExitReason.POSITION_IDENTITY_CHANGED
                runtime = PositionRuntimeState.from_position(position, current_close)
                self._state.active_position = runtime
                runtime_initialized = True
            else:
                observed_quantity = _decimal(position.quantity)
                previous_quantity = runtime.last_seen_quantity
                quantity_increased = observed_quantity > previous_quantity
                entry_price_changed = _decimal(position.entry_price) != runtime.entry_price
                if self.config.reset_on_scale_in and (quantity_increased or entry_price_changed):
                    reason = PositionExitReason.SCALE_IN_RESET
                    runtime = PositionRuntimeState.from_position(position, current_close)
                    self._state.active_position = runtime
                    runtime_initialized = True
                    diagnostics["scale_in_quantity_increased"] = quantity_increased
                    diagnostics["scale_in_entry_price_changed"] = entry_price_changed
                else:
                    if quantity_increased and runtime.pending_partial_target_quantity is not None:
                        increase = observed_quantity - previous_quantity
                        runtime.pending_partial_target_quantity += increase
                        diagnostics["pending_partial_scale_in_adjustment"] = float(increase)
                    self._reconcile_partial(position.quantity, diagnostics)

        bars_since_fill: int | None = None
        if lifecycle is not None:
            bars_since_fill = view.index - lifecycle.entry_index
            if bars_since_fill < 0:
                raise ValueError("position lifecycle entry_index cannot exceed current view index")
            if (
                runtime_initialized
                and bars_since_fill > 0
                and lifecycle.no_progress_bars is not None
            ):
                raise ValueError(
                    "cannot initialize no-progress lifecycle after the position fill bar"
                )
            if runtime_initialized:
                runtime.favorable_extreme = runtime.entry_price
            diagnostics["bars_since_fill"] = bars_since_fill
            diagnostics["lifecycle_max_hold_bars"] = lifecycle.max_hold_bars
            diagnostics["lifecycle_no_progress_bars"] = lifecycle.no_progress_bars
            diagnostics["lifecycle_no_progress_min_mfe_r"] = (
                None
                if lifecycle.no_progress_min_mfe_r is None
                else str(lifecycle.no_progress_min_mfe_r)
            )

        runtime.last_stop = (
            _decimal(position.stop_loss) if position.stop_loss is not None else runtime.last_stop
        )
        runtime.last_seen_quantity = _decimal(position.quantity)
        runtime.bars_managed += 1

        held_for = close_time - runtime.entry_time
        diagnostics["holding_minutes"] = max(0.0, held_for.total_seconds() / 60.0)
        diagnostics["bars_managed"] = runtime.bars_managed
        if held_for.total_seconds() < 0:
            return self._decision(
                view,
                reason=PositionExitReason.FUTURE_ENTRY_TIME,
                skips=(PositionExitReason.FUTURE_ENTRY_TIME.value,),
                diagnostics=diagnostics,
            )

        time_exit = self._time_exit_request(view, runtime, held_for, diagnostics)
        if time_exit is not None:
            action = (
                ManagementAction.TIME_EXIT_RETRY
                if runtime.full_close_requested and diagnostics.get("time_exit_retry") is True
                else ManagementAction.TIME_EXIT
            )
            exit_reason = (
                PositionExitReason.TIME_EXIT_RETRY
                if action is ManagementAction.TIME_EXIT_RETRY
                else PositionExitReason.TIME_EXIT
            )
            return self._decision(
                view,
                reason=exit_reason,
                action=action,
                closes=(time_exit,),
                diagnostics=diagnostics,
            )

        if view.exit_pending:
            return self._decision(
                view,
                reason=PositionExitReason.CLOSE_PENDING,
                skips=(PositionExitReason.CLOSE_PENDING.value,),
                diagnostics=diagnostics,
            )

        if self.config.suspend_price_actions_on_gap and view.data_gap:
            current_r = self._current_r(runtime, view)
            diagnostics["current_r"] = None if current_r is None else float(current_r)
            diagnostics["favorable_extreme"] = float(runtime.favorable_extreme)
            if runtime.pending_partial_index is not None:
                partial_retry = self._partial_exit_request(
                    view,
                    runtime,
                    None,
                    diagnostics,
                )
                if partial_retry is not None:
                    return self._decision(
                        view,
                        reason=PositionExitReason.PARTIAL_EXIT_RETRY,
                        action=ManagementAction.PARTIAL_EXIT_RETRY,
                        closes=(partial_retry,),
                        skips=(PositionExitReason.DATA_GAP.value,),
                        diagnostics=diagnostics,
                        current_r=current_r,
                    )
            return self._decision(
                view,
                reason=PositionExitReason.DATA_GAP,
                skips=(PositionExitReason.DATA_GAP.value,),
                diagnostics=diagnostics,
                current_r=current_r,
            )

        if bars_since_fill != 0:
            self._update_favorable_extreme(runtime, view)
        current_r = self._current_r(runtime, view)
        diagnostics["current_r"] = None if current_r is None else float(current_r)
        diagnostics["favorable_extreme"] = float(runtime.favorable_extreme)

        lifecycle_exit = self._position_lifecycle_exit(
            runtime,
            lifecycle,
            bars_since_fill,
            diagnostics,
        )
        if lifecycle_exit is not None:
            close_request, lifecycle_reason, lifecycle_action = lifecycle_exit
            return self._decision(
                view,
                reason=lifecycle_reason,
                action=lifecycle_action,
                closes=(close_request,),
                diagnostics=diagnostics,
                current_r=current_r,
            )

        skips: list[str] = []
        if current_r is None and self._requires_initial_risk():
            skips.append(PositionExitReason.MISSING_INITIAL_RISK.value)
        partial = self._partial_exit_request(view, runtime, current_r, diagnostics)
        stop_update, stop_action = self._protective_stop_update(
            view,
            runtime,
            current_r,
            diagnostics,
        )
        action = ManagementAction.NONE
        result_reason = reason
        if partial is not None:
            action = (
                ManagementAction.PARTIAL_EXIT_RETRY
                if diagnostics.get("partial_retry") is True
                else ManagementAction.PARTIAL_EXIT
            )
            result_reason = (
                PositionExitReason.PARTIAL_EXIT_RETRY
                if action is ManagementAction.PARTIAL_EXIT_RETRY
                else PositionExitReason.PARTIAL_EXIT
            )
        elif stop_action is not None:
            action = stop_action
            result_reason = PositionExitReason.PROTECTIVE_STOP_UPDATE
        return self._decision(
            view,
            reason=result_reason,
            action=action,
            closes=(() if partial is None else (partial,)),
            position_updates=(() if stop_update is None else (stop_update,)),
            skips=tuple(dict.fromkeys(skips)),
            diagnostics=diagnostics,
            current_r=current_r,
        )

    def _routine_monitoring_fast_path(
        self,
        view: MarketView,
        runtime: PositionRuntimeState,
        close_time: datetime,
    ) -> ModuleDecision | None:
        if view.exit_pending or view.data_gap or close_time < runtime.entry_time:
            return None
        position = view.position
        if position is None:
            return None
        quantity_increased = position.quantity > float(runtime.last_seen_quantity)
        entry_price_changed = position.entry_price != float(runtime.entry_price)
        if self.config.reset_on_scale_in and (quantity_increased or entry_price_changed):
            return None
        if position.stop_loss is not None and (
            runtime.last_stop is None or position.stop_loss != float(runtime.last_stop)
        ):
            runtime.last_stop = _decimal(position.stop_loss)
        if position.quantity != float(runtime.last_seen_quantity):
            runtime.last_seen_quantity = _decimal(position.quantity)
        runtime.bars_managed += 1
        return ModuleDecision()

    def _handle_flat(self, view: MarketView) -> ModuleDecision:
        previous = self._state.active_position
        candidates = []
        for order in view.pending_orders:
            managed = self.config.cancel_reduce_only_orders_on_flat and order.reduce_only
            if (
                previous is not None
                and previous.setup_id is not None
                and order.setup_id == previous.setup_id
            ):
                managed = True
            if self.config.cancel_managed_tag_orders_on_flat and order.tag.startswith(
                self.config.managed_order_tag_prefix
            ):
                managed = True
            if managed:
                candidates.append(order)

        candidate_ids = {order.id for order in candidates}
        self._state.cancel_attempts = {
            order_id: index
            for order_id, index in self._state.cancel_attempts.items()
            if order_id in candidate_ids
        }
        eligible = (
            order
            for order in candidates
            if (last_attempt := self._state.cancel_attempts.get(order.id)) is None
            or view.index - last_attempt >= self.config.cancel_retry_bars
        )
        selected = nsmallest(
            self.config.max_cancel_requests_per_bar,
            eligible,
            key=lambda item: item.id,
        )
        cancellations = [
            CancelRequest(
                order_id=order.id,
                reason=PositionExitReason.CANCEL_ORPHAN_ORDER.value,
            )
            for order in selected
        ]
        for order in selected:
            self._state.cancel_attempts[order.id] = view.index
        self._bound_cancel_attempts()

        if not candidates:
            self._state.active_position = None
        diagnostics: dict[str, object] = {
            "managed_pending_orders": len(candidates),
            "cancel_requests": len(cancellations),
            "cancel_limit": self.config.max_cancel_requests_per_bar,
        }
        if previous is not None:
            diagnostics["closed_position_id"] = previous.position_id
        skips: tuple[str, ...] = ()
        action = ManagementAction.NONE
        reason = PositionExitReason.POSITION_FLAT
        if cancellations:
            action = ManagementAction.CANCEL_ORPHAN_ORDER
            reason = PositionExitReason.CANCEL_ORPHAN_ORDER
        elif self.config.emit_idle_skips:
            skips = (PositionExitReason.NO_POSITION.value,)
            reason = PositionExitReason.NO_POSITION
        return self._decision(
            view,
            reason=reason,
            action=action,
            cancellations=tuple(cancellations),
            skips=skips,
            diagnostics=diagnostics,
        )

    def _reconcile_partial(
        self,
        current_quantity: float,
        diagnostics: dict[str, object],
    ) -> None:
        runtime = self._state.active_position
        if runtime is None:
            raise RuntimeError("partial reconciliation requires active position state")
        if runtime.pending_partial_index is None:
            return
        target = runtime.pending_partial_target_quantity
        if target is None:
            raise RuntimeError("pending partial state requires target quantity")
        quantity = _decimal(current_quantity)
        tolerance_confirmed = (
            quantity < runtime.last_seen_quantity
            and quantity <= target + _float_transport_tolerance(quantity, target)
        )
        if quantity <= target or tolerance_confirmed:
            index = runtime.pending_partial_index
            runtime.completed_partial_indices.add(index)
            runtime.pending_partial_index = None
            runtime.pending_partial_target_quantity = None
            diagnostics["partial_completed_index"] = index

    def _position_lifecycle_rule(
        self,
        view: MarketView,
    ) -> _PositionLifecycleRule | None:
        if not self.config.honor_position_lifecycle_metadata:
            return None
        position = view.position
        if position is None:
            return None
        metadata = position.metadata
        max_raw = metadata.get("router_max_holding_seconds")
        no_progress_after_raw = metadata.get("router_no_progress_after_seconds")
        no_progress_mfe_raw = metadata.get("router_no_progress_minimum_mfe_r")
        if max_raw is None and no_progress_after_raw is None and no_progress_mfe_raw is None:
            return None
        if position.entry_index is None:
            raise ValueError("position lifecycle metadata requires entry_index")

        bar_seconds_float = (view.current.close_time - view.current.open_time).total_seconds()
        if (
            not math.isfinite(bar_seconds_float)
            or bar_seconds_float <= 0
            or not bar_seconds_float.is_integer()
        ):
            raise ValueError("position lifecycle requires an integral positive bar duration")
        bar_seconds = int(bar_seconds_float)

        max_hold_bars = (
            None
            if max_raw is None
            else self._lifecycle_seconds_to_bars(
                max_raw,
                bar_seconds,
                "router_max_holding_seconds",
            )
        )

        no_progress_bars: int | None = None
        no_progress_min_mfe_r: Decimal | None = None
        if (no_progress_after_raw is None) != (no_progress_mfe_raw is None):
            raise ValueError(
                "position no-progress lifecycle requires both after-seconds and minimum-MFE-R"
            )
        if no_progress_after_raw is not None:
            no_progress_bars = self._lifecycle_seconds_to_bars(
                no_progress_after_raw,
                bar_seconds,
                "router_no_progress_after_seconds",
            )
            if not isinstance(no_progress_mfe_raw, str) or not no_progress_mfe_raw:
                raise ValueError(
                    "router_no_progress_minimum_mfe_r must be a canonical decimal string"
                )
            try:
                no_progress_min_mfe_r = Decimal(no_progress_mfe_raw)
            except Exception as exc:
                raise ValueError(
                    "router_no_progress_minimum_mfe_r must be a canonical decimal string"
                ) from exc
            if (
                not no_progress_min_mfe_r.is_finite()
                or no_progress_min_mfe_r < 0
                or str(no_progress_min_mfe_r) != no_progress_mfe_raw
            ):
                raise ValueError(
                    "router_no_progress_minimum_mfe_r must be a canonical non-negative decimal"
                )
            if max_hold_bars is not None and no_progress_bars >= max_hold_bars:
                raise ValueError("position no-progress lifecycle must precede maximum hold")

        return _PositionLifecycleRule(
            entry_index=position.entry_index,
            max_hold_bars=max_hold_bars,
            no_progress_bars=no_progress_bars,
            no_progress_min_mfe_r=no_progress_min_mfe_r,
        )

    @staticmethod
    def _lifecycle_seconds_to_bars(
        value: object,
        bar_seconds: int,
        name: str,
    ) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ValueError(f"{name} must be a positive integer")
        if value % bar_seconds:
            raise ValueError(f"{name} must be an exact multiple of the current bar duration")
        bars = value // bar_seconds
        if bars < 1:
            raise ValueError(f"{name} must span at least one bar")
        return bars

    def _position_lifecycle_exit(
        self,
        runtime: PositionRuntimeState,
        lifecycle: _PositionLifecycleRule | None,
        bars_since_fill: int | None,
        diagnostics: dict[str, object],
    ) -> tuple[ClosePositionRequest, PositionExitReason, ManagementAction] | None:
        if lifecycle is None or bars_since_fill is None or bars_since_fill == 0:
            return None

        if lifecycle.no_progress_bars is not None and bars_since_fill == lifecycle.no_progress_bars:
            threshold = lifecycle.no_progress_min_mfe_r
            risk = runtime.initial_risk_per_unit
            if threshold is None or risk is None:
                raise ValueError("no-progress lifecycle requires valid initial risk")
            entry = float(runtime.entry_price)
            extreme = float(runtime.favorable_extreme)
            risk_value = float(risk)
            if not all(math.isfinite(value) for value in (entry, extreme, risk_value)):
                raise ValueError("no-progress lifecycle values must be finite")
            move = extreme - entry if runtime.side is PositionSide.LONG else entry - extreme
            mfe_r = max(0.0, move / risk_value)
            diagnostics["lifecycle_mfe_r"] = mfe_r
            if mfe_r < float(threshold):
                return (
                    ClosePositionRequest(reason="no_progress"),
                    PositionExitReason.NO_PROGRESS_EXIT,
                    ManagementAction.NO_PROGRESS_EXIT,
                )

        if lifecycle.max_hold_bars is not None and bars_since_fill >= lifecycle.max_hold_bars - 1:
            return (
                ClosePositionRequest(reason="max_hold"),
                PositionExitReason.MAX_HOLD_EXIT,
                ManagementAction.MAX_HOLD_EXIT,
            )
        return None

    def _time_exit_request(
        self,
        view: MarketView,
        runtime: PositionRuntimeState,
        held_for: timedelta,
        diagnostics: dict[str, object],
    ) -> ClosePositionRequest | None:
        limit = self.config.max_holding_minutes
        if limit is None or held_for < timedelta(minutes=limit) or view.exit_pending:
            return None
        retry = runtime.full_close_requested
        runtime.full_close_requested = True
        diagnostics["time_exit_retry"] = retry
        return ClosePositionRequest(
            reason=(
                PositionExitReason.TIME_EXIT_RETRY.value
                if retry
                else PositionExitReason.TIME_EXIT.value
            )
        )

    def _partial_exit_request(
        self,
        view: MarketView,
        runtime: PositionRuntimeState,
        current_r: Decimal | None,
        diagnostics: dict[str, object],
    ) -> ClosePositionRequest | None:
        position = view.position
        if position is None:
            raise RuntimeError("partial exit requires an open position")
        current_quantity = _decimal(position.quantity)
        if runtime.pending_partial_index is not None:
            target = runtime.pending_partial_target_quantity
            if target is None:
                raise RuntimeError("pending partial state requires target quantity")
            residual = current_quantity - target
            if residual <= 0:
                runtime.completed_partial_indices.add(runtime.pending_partial_index)
                runtime.pending_partial_index = None
                runtime.pending_partial_target_quantity = None
                return None
            encoded_residual = _partial_transport_quantity(
                current_quantity,
                residual,
                target,
            )
            if encoded_residual is None:
                diagnostics["partial_transport_unrepresentable"] = True
                return None
            diagnostics["partial_retry"] = True
            diagnostics["partial_index"] = runtime.pending_partial_index
            diagnostics["partial_quantity"] = encoded_residual
            return ClosePositionRequest(
                reason=(
                    f"{PositionExitReason.PARTIAL_EXIT_RETRY.value}."
                    f"stage_{runtime.pending_partial_index + 1}"
                ),
                quantity=encoded_residual,
            )
        if current_r is None:
            return None
        for index, rule in enumerate(self.config.partial_exits):
            if index in runtime.completed_partial_indices or current_r < rule.trigger_r:
                continue
            requested = min(current_quantity, runtime.initial_quantity * rule.fraction)
            target = max(Decimal("0"), current_quantity - requested)
            encoded_requested = _partial_transport_quantity(
                current_quantity,
                requested,
                target,
            )
            if encoded_requested is None:
                diagnostics["partial_transport_unrepresentable"] = True
                diagnostics["partial_index"] = index
                return None
            runtime.pending_partial_index = index
            runtime.pending_partial_target_quantity = target
            diagnostics["partial_retry"] = False
            diagnostics["partial_index"] = index
            diagnostics["partial_trigger_r"] = float(rule.trigger_r)
            diagnostics["partial_quantity"] = encoded_requested
            return ClosePositionRequest(
                reason=f"{PositionExitReason.PARTIAL_EXIT.value}.stage_{index + 1}",
                quantity=encoded_requested,
            )
        return None

    def _protective_stop_update(
        self,
        view: MarketView,
        runtime: PositionRuntimeState,
        current_r: Decimal | None,
        diagnostics: dict[str, object],
    ) -> tuple[PositionUpdateRequest | None, ManagementAction | None]:
        position = view.position
        if position is None:
            raise RuntimeError("protective stop update requires an open position")
        candidates: list[tuple[ManagementAction, Decimal]] = []
        risk = runtime.initial_risk_per_unit
        if (
            self.config.breakeven_trigger_r is not None
            and risk is not None
            and (
                runtime.breakeven_applied
                or (current_r is not None and current_r >= self.config.breakeven_trigger_r)
            )
        ):
            offset = risk * self.config.breakeven_offset_r
            candidates.append(
                (
                    ManagementAction.BREAKEVEN,
                    runtime.entry_price + offset
                    if runtime.side is PositionSide.LONG
                    else runtime.entry_price - offset,
                )
            )
        if self.config.trailing_mode is not TrailingMode.NONE:
            can_activate_without_r = (
                self.config.trailing_mode is TrailingMode.PERCENT
                and self.config.trailing_activation_r == 0
            )
            if (
                runtime.trailing_active
                or can_activate_without_r
                or (current_r is not None and current_r >= self.config.trailing_activation_r)
            ):
                candidate = self._trailing_candidate(runtime)
                if candidate is not None:
                    candidates.append((ManagementAction.TRAILING, candidate))
        if not candidates:
            return None, None

        selected_action, raw_candidate = candidates[0]
        for action, value in candidates[1:]:
            is_tighter = (
                value > raw_candidate
                if runtime.side is PositionSide.LONG
                else value < raw_candidate
            )
            if is_tighter or (value == raw_candidate and action is ManagementAction.TRAILING):
                selected_action, raw_candidate = action, value
        market_close = _decimal(view.current.close)
        candidate = self._buffer_behind_market(
            raw_candidate,
            market_close,
            runtime.side,
        )
        try:
            encoded_candidate = float(candidate)
            encoded_market = float(market_close)
        except (OverflowError, ValueError):
            return None, None
        encoded_is_behind_market = (
            encoded_candidate < encoded_market
            if runtime.side is PositionSide.LONG
            else encoded_candidate > encoded_market
        )
        if (
            not math.isfinite(encoded_candidate)
            or encoded_candidate <= 0
            or not encoded_is_behind_market
        ):
            return None, None
        candidate = Decimal(str(encoded_candidate))
        existing = None if position.stop_loss is None else _decimal(position.stop_loss)
        if not self._improves_stop(existing, candidate, runtime.side):
            if existing is not None:
                self._record_satisfied_stop_components(runtime, existing, candidates)
            return None, None

        self._record_satisfied_stop_components(runtime, candidate, candidates)
        runtime.last_stop = candidate
        diagnostics["requested_stop"] = float(candidate)
        diagnostics["stop_components"] = tuple(action.value for action, _ in candidates)
        return (
            PositionUpdateRequest(
                stop_loss=float(candidate),
                take_profit=position.take_profit,
                reason=(
                    f"{PositionExitReason.PROTECTIVE_STOP_UPDATE.value}.{selected_action.value}"
                ),
            ),
            selected_action,
        )

    def _update_favorable_extreme(
        self,
        runtime: PositionRuntimeState,
        view: MarketView,
    ) -> None:
        if runtime.side is PositionSide.LONG:
            runtime.favorable_extreme = max(
                runtime.favorable_extreme,
                _decimal(view.current.high),
            )
        else:
            runtime.favorable_extreme = min(
                runtime.favorable_extreme,
                _decimal(view.current.low),
            )

    def _current_r(
        self,
        runtime: PositionRuntimeState,
        view: MarketView,
    ) -> Decimal | None:
        risk = runtime.initial_risk_per_unit
        if risk is None:
            return None
        if self.config.trigger_source is TriggerPriceSource.CLOSE:
            price = _decimal(view.current.close)
        else:
            price = _decimal(
                view.current.high if runtime.side is PositionSide.LONG else view.current.low
            )
        move = (
            price - runtime.entry_price
            if runtime.side is PositionSide.LONG
            else runtime.entry_price - price
        )
        return move / risk

    def _trailing_candidate(
        self,
        runtime: PositionRuntimeState,
    ) -> Decimal | None:
        if self.config.trailing_mode is TrailingMode.R_MULTIPLE:
            risk = runtime.initial_risk_per_unit
            if risk is None:
                return None
            distance = risk * self.config.trailing_distance_r
        elif self.config.trailing_mode is TrailingMode.PERCENT:
            distance = runtime.favorable_extreme * self.config.trailing_distance_pct
        else:
            return None
        return (
            runtime.favorable_extreme - distance
            if runtime.side is PositionSide.LONG
            else runtime.favorable_extreme + distance
        )

    def _buffer_behind_market(
        self,
        candidate: Decimal,
        mark: Decimal,
        side: PositionSide,
    ) -> Decimal:
        configured = self.config.market_buffer_bps / Decimal("10000")
        strict = max(configured, Decimal("1e-12"))
        if side is PositionSide.LONG:
            return min(candidate, mark * (Decimal("1") - strict))
        return max(candidate, mark * (Decimal("1") + strict))

    def _improves_stop(
        self,
        existing: Decimal | None,
        candidate: Decimal,
        side: PositionSide,
    ) -> bool:
        if not candidate.is_finite() or candidate <= 0:
            return False
        if existing is None:
            return True
        threshold = existing * self.config.minimum_stop_move_bps / Decimal("10000")
        if side is PositionSide.LONG:
            return candidate > existing + threshold
        return candidate < existing - threshold

    def _requires_initial_risk(self) -> bool:
        if self.config.partial_exits or self.config.breakeven_trigger_r is not None:
            return True
        return self.config.trailing_mode is TrailingMode.R_MULTIPLE or (
            self.config.trailing_mode is TrailingMode.PERCENT
            and self.config.trailing_activation_r > 0
        )

    def _bound_cancel_attempts(self) -> None:
        limit = self.config.max_tracked_cancels
        if len(self._state.cancel_attempts) <= limit:
            return
        ordered = sorted(
            self._state.cancel_attempts.items(),
            key=lambda item: (item[1], item[0]),
        )
        self._state.cancel_attempts = dict(ordered[-limit:])

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
        details = dict(diagnostics or {})
        features: tuple[Feature, ...] = ()
        emit_status = (
            self.config.emit_status_feature
            or action is not ManagementAction.NONE
            or reason
            not in {
                PositionExitReason.POSITION_MONITORED,
                PositionExitReason.POSITION_INITIALIZED,
                PositionExitReason.POSITION_FLAT,
            }
            or "closed_position_id" in details
        )
        if emit_status:
            features = (
                self._status_feature(
                    view,
                    reason=reason,
                    action=action,
                    current_r=current_r,
                    diagnostics=details,
                ),
            )
        return ModuleDecision(
            features=features,
            cancellations=cancellations,
            position_updates=position_updates,
            closes=closes,
            skips=skips,
            diagnostics=details,
        )

    def _status_feature(
        self,
        view: MarketView,
        *,
        reason: PositionExitReason,
        action: ManagementAction,
        current_r: Decimal | None,
        diagnostics: Mapping[str, object],
    ) -> Feature:
        current = view.current
        runtime = self._state.active_position
        position = view.position
        status = PositionManagementStatus(
            position_id=None if position is None else position.id,
            setup_id=None if position is None else position.setup_id,
            symbol=current.symbol,
            timeframe=current.timeframe,
            side=None if position is None else position.side,
            quantity=None if position is None else _decimal(position.quantity),
            entry_price=None if position is None else _decimal(position.entry_price),
            initial_risk_per_unit=(None if runtime is None else runtime.initial_risk_per_unit),
            current_r=current_r,
            favorable_extreme=None if runtime is None else runtime.favorable_extreme,
            stop_loss=(
                None
                if position is None or position.stop_loss is None
                else _decimal(position.stop_loss)
            ),
            take_profit=(
                None
                if position is None or position.take_profit is None
                else _decimal(position.take_profit)
            ),
            completed_partial_indices=(
                () if runtime is None else tuple(sorted(runtime.completed_partial_indices))
            ),
            pending_partial_index=(None if runtime is None else runtime.pending_partial_index),
            trailing_active=False if runtime is None else runtime.trailing_active,
            breakeven_applied=False if runtime is None else runtime.breakeven_applied,
            action=action,
            reason=reason,
            created_at=current.close_time,
            diagnostics=diagnostics,
        )
        value = status.as_mapping()
        feature_id = _status_feature_id(value)
        return Feature(
            key=STATUS_FEATURE_KEY,
            source_module=MODULE_NAME,
            created_at=current.close_time,
            valid_until=current.close_time,
            value=value,
            symbol=current.symbol,
            timeframe=current.timeframe,
            schema_version=FEATURE_SCHEMA_VERSION,
            feature_id=feature_id,
            invalidate_on_gap=False,
        )

    @classmethod
    def _record_satisfied_stop_components(
        cls,
        runtime: PositionRuntimeState,
        actual: Decimal,
        candidates: Sequence[tuple[ManagementAction, Decimal]],
    ) -> None:
        for action, requested in candidates:
            satisfied = cls._stop_at_least(actual, requested, runtime.side)
            if action is ManagementAction.BREAKEVEN and satisfied:
                runtime.breakeven_applied = True
            if action is ManagementAction.TRAILING and satisfied:
                runtime.trailing_active = True

    @staticmethod
    def _stop_at_least(
        actual: Decimal,
        requested: Decimal,
        side: PositionSide,
    ) -> bool:
        return actual >= requested if side is PositionSide.LONG else actual <= requested


def _validate_view(view: MarketView) -> None:
    if isinstance(view.index, bool) or not isinstance(view.index, int) or view.index < 0:
        raise ValueError("view.index must be a non-negative integer")
    if not isinstance(view.exit_pending, bool):
        raise TypeError("view.exit_pending must be boolean")
    if not isinstance(view.data_gap, bool):
        raise TypeError("view.data_gap must be boolean")
    if not isinstance(view.pending_orders, tuple):
        raise TypeError("view.pending_orders must be a tuple")

    position = view.position
    if position is not None:
        _require_nonblank_input(position.id, "position.id")
        if not isinstance(position.side, PositionSide):
            raise TypeError("position.side must be PositionSide")
        _require_positive_input_number(position.quantity, "position.quantity")
        _require_positive_input_number(position.entry_price, "position.entry_price")
        _require_aware_input_time(position.entry_time, "position.entry_time")
        if position.stop_loss is not None:
            _require_positive_input_number(position.stop_loss, "position.stop_loss")
        if position.take_profit is not None:
            _require_positive_input_number(position.take_profit, "position.take_profit")
        if position.setup_id is not None:
            _require_nonblank_input(position.setup_id, "position.setup_id")
        if position.entry_index is not None:
            if isinstance(position.entry_index, bool) or not isinstance(position.entry_index, int):
                raise TypeError("position.entry_index must be int or None")
            if position.entry_index < 0:
                raise ValueError("position.entry_index cannot be negative")
        if not isinstance(position.metadata, Mapping):
            raise TypeError("position.metadata must be a mapping")

    seen_order_ids: set[str] = set()
    for order in view.pending_orders:
        _require_nonblank_input(order.id, "pending_order.id")
        if order.id in seen_order_ids:
            raise ValueError("pending_order.id values must be unique")
        seen_order_ids.add(order.id)
        if not isinstance(order.reduce_only, bool):
            raise TypeError("pending_order.reduce_only must be boolean")
        if not isinstance(order.tag, str):
            raise TypeError("pending_order.tag must be a string")
        if len(order.tag) > 4096:
            raise ValueError("pending_order.tag exceeds 4096 characters")
        if order.setup_id is not None:
            _require_nonblank_input(order.setup_id, "pending_order.setup_id")


def _require_nonblank_input(
    value: object,
    name: str,
    *,
    max_length: int = 256,
) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-blank string")
    if value != value.strip():
        raise ValueError(f"{name} cannot contain surrounding whitespace")
    if len(value) > max_length:
        raise ValueError(f"{name} exceeds {max_length} characters")


def _require_positive_input_number(value: object, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int | float | Decimal):
        raise TypeError(f"{name} must be numeric")
    if isinstance(value, Decimal):
        valid = value.is_finite() and value > 0
    else:
        valid = math.isfinite(value) and value > 0
    if not valid:
        raise ValueError(f"{name} must be finite and > 0")


def _require_aware_input_time(value: object, name: str) -> None:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _event_fingerprint(view: MarketView) -> str:
    current = view.current
    position = view.position
    position_identity = (
        None
        if position is None
        else (
            position.id,
            position.side.value,
            position.quantity,
            position.entry_price,
            position.entry_time.astimezone(UTC).isoformat(),
            position.stop_loss,
            position.take_profit,
            position.setup_id,
            position.entry_index,
            position.metadata.get("router_max_holding_seconds"),
            position.metadata.get("router_no_progress_after_seconds"),
            position.metadata.get("router_no_progress_minimum_mfe_r"),
        )
    )
    order_identity = (
        ()
        if position is not None
        else tuple(
            sorted(
                (order.id, order.reduce_only, order.setup_id, order.tag)
                for order in view.pending_orders
            )
        )
    )
    payload = repr(
        (
            "position-exit-event-v2",
            view.index,
            current.symbol,
            current.timeframe,
            current.timestamp.isoformat(),
            current.open,
            current.high,
            current.low,
            current.close,
            current.volume,
            view.data_gap,
            view.exit_pending,
            position_identity,
            order_identity,
        )
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _status_feature_id(value: Mapping[str, object]) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:32]
    return f"position-exit-status-{digest}"


def _decimal(value: int | float | Decimal) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))
