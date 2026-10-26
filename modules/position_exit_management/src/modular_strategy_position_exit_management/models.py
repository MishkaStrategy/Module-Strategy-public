from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from modular_core import PositionSide

from .constants import FEATURE_SCHEMA_VERSION
from .decimal_utils import optional_exact_decimal_text, validate_decimal_bounds


class TrailingMode(StrEnum):
    NONE = "none"
    R_MULTIPLE = "r_multiple"
    PERCENT = "percent"


class TriggerPriceSource(StrEnum):
    CLOSE = "close"
    EXTREME = "extreme"


class PositionExitReason(StrEnum):
    POSITION_MONITORED = "position_exit_management.position_monitored"
    POSITION_INITIALIZED = "position_exit_management.position_initialized"
    POSITION_REPLACED = "position_exit_management.position_replaced"
    POSITION_IDENTITY_CHANGED = "position_exit_management.position_identity_changed"
    SCALE_IN_RESET = "position_exit_management.scale_in_reset"
    TIME_EXIT = "position_exit_management.time_exit"
    TIME_EXIT_RETRY = "position_exit_management.time_exit_retry"
    NO_PROGRESS_EXIT = "position_exit_management.no_progress_exit"
    MAX_HOLD_EXIT = "position_exit_management.max_hold_exit"
    PARTIAL_EXIT = "position_exit_management.partial_exit"
    PARTIAL_EXIT_RETRY = "position_exit_management.partial_exit_retry"
    PARTIAL_EXIT_CONFIRMED = "position_exit_management.partial_exit_confirmed"
    PROTECTIVE_STOP_UPDATE = "position_exit_management.protective_stop_update"
    CLOSE_PENDING = "position_exit_management.close_pending"
    DATA_GAP = "position_exit_management.data_gap"
    MISSING_INITIAL_RISK = "position_exit_management.missing_initial_risk"
    FUTURE_ENTRY_TIME = "position_exit_management.future_entry_time"
    NO_POSITION = "position_exit_management.no_position"
    POSITION_FLAT = "position_exit_management.position_flat"
    CANCEL_ORPHAN_ORDER = "position_exit_management.cancel_orphan_order"
    DUPLICATE_EVENT = "position_exit_management.duplicate_event"
    CONFLICTING_DUPLICATE = "position_exit_management.conflicting_duplicate"
    OUT_OF_ORDER_EVENT = "position_exit_management.out_of_order_event"
    STREAM_CHANGED = "position_exit_management.stream_changed"
    STREAM_CHANGED_WITH_POSITION = "position_exit_management.stream_changed_with_position"
    STATE_RESTORED = "position_exit_management.state_restored"
    STATE_RESET = "position_exit_management.state_reset"


class ManagementAction(StrEnum):
    NONE = "none"
    TIME_EXIT = "time_exit"
    TIME_EXIT_RETRY = "time_exit_retry"
    NO_PROGRESS_EXIT = "no_progress_exit"
    MAX_HOLD_EXIT = "max_hold_exit"
    PARTIAL_EXIT = "partial_exit"
    PARTIAL_EXIT_RETRY = "partial_exit_retry"
    BREAKEVEN = "breakeven"
    TRAILING = "trailing"
    CANCEL_ORPHAN_ORDER = "cancel_orphan_order"


@dataclass(frozen=True, slots=True, order=True)
class PartialExitRule:
    trigger_r: Decimal
    fraction: Decimal

    def __post_init__(self) -> None:
        validate_decimal_bounds(self.trigger_r, "partial exit trigger_r")
        validate_decimal_bounds(self.fraction, "partial exit fraction")
        if self.trigger_r < 0:
            raise ValueError("partial exit trigger_r must be >= 0")
        if not Decimal("0") < self.fraction <= Decimal("1"):
            raise ValueError("partial exit fraction must be in (0, 1]")


@dataclass(frozen=True, slots=True)
class PositionManagementStatus:
    position_id: str | None
    setup_id: str | None
    symbol: str
    timeframe: str
    side: PositionSide | None
    quantity: Decimal | None
    entry_price: Decimal | None
    initial_risk_per_unit: Decimal | None
    current_r: Decimal | None
    favorable_extreme: Decimal | None
    stop_loss: Decimal | None
    take_profit: Decimal | None
    completed_partial_indices: tuple[int, ...]
    pending_partial_index: int | None
    trailing_active: bool
    breakeven_applied: bool
    action: ManagementAction
    reason: PositionExitReason
    created_at: datetime
    diagnostics: Mapping[str, object]

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "position_id": self.position_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": None if self.side is None else self.side.value,
            "quantity": optional_exact_decimal_text(self.quantity),
            "entry_price": optional_exact_decimal_text(self.entry_price),
            "initial_risk_per_unit": optional_exact_decimal_text(self.initial_risk_per_unit),
            "current_r": optional_exact_decimal_text(self.current_r),
            "favorable_extreme": optional_exact_decimal_text(self.favorable_extreme),
            "stop_loss": optional_exact_decimal_text(self.stop_loss),
            "take_profit": optional_exact_decimal_text(self.take_profit),
            "completed_partial_indices": self.completed_partial_indices,
            "pending_partial_index": self.pending_partial_index,
            "trailing_active": self.trailing_active,
            "breakeven_applied": self.breakeven_applied,
            "action": self.action.value,
            "reason": self.reason.value,
            "created_at": self.created_at.isoformat(),
            "diagnostics": dict(self.diagnostics),
        }
