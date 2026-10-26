from __future__ import annotations

from enum import Enum


class ExecutionMode(str, Enum):
    PAPER = "paper"


class PlanAction(str, Enum):
    OPEN_POSITION = "open_position"
    ADD_TO_POSITION = "add_to_position"
    REDUCE_POSITION = "reduce_position"
    CLOSE_POSITION = "close_position"
    CANCEL_ORDERS = "cancel_orders"
    REPLACE_ORDER = "replace_order"
    REVERSE_POSITION = "reverse_position"


class IntentRole(str, Enum):
    ENTRY = "entry"
    STOP = "stop"
    TARGET = "target"
    CANCEL = "cancel"
    REPLACEMENT = "replacement"


class PaperOrderState(str, Enum):
    CREATED = "created"
    SUBMITTED = "submitted"
    ACKNOWLEDGED = "acknowledged"
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCEL_PENDING = "cancel_pending"
    CANCELED = "canceled"
    EXPIRED = "expired"
    REJECTED = "rejected"
    ERROR = "error"

    @property
    def terminal(self) -> bool:
        return self in {
            PaperOrderState.FILLED,
            PaperOrderState.CANCELED,
            PaperOrderState.EXPIRED,
            PaperOrderState.REJECTED,
            PaperOrderState.ERROR,
        }


class ReconciliationMode(str, Enum):
    REPORT_ONLY = "report_only"
    SAFE_AUTO_REPAIR = "safe_auto_repair"
    STRICT_BLOCK = "strict_block"
    RECOVERY_MODE = "recovery_mode"


class FindingSeverity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    HIGH = "high"
    CRITICAL = "critical"


class PaperReasonCode(str, Enum):
    LIVE_EXECUTION_NOT_SUPPORTED = "LIVE_EXECUTION_NOT_SUPPORTED"
    INVALID_CONFIGURATION = "INVALID_CONFIGURATION"
    INVALID_PLAN = "INVALID_PLAN"
    UNSUPPORTED_PLAN_SCHEMA = "UNSUPPORTED_PLAN_SCHEMA"
    PLAN_HASH_MISMATCH = "PLAN_HASH_MISMATCH"
    PLAN_ID_CONTENT_MISMATCH = "PLAN_ID_CONTENT_MISMATCH"
    PLAN_NOT_YET_VALID = "PLAN_NOT_YET_VALID"
    STALE_PLAN = "STALE_PLAN"
    STATE_VERSION_MISMATCH = "STATE_VERSION_MISMATCH"
    UNSUPPORTED_PAPER_ORDER_TYPE = "UNSUPPORTED_PAPER_ORDER_TYPE"
    VENUE_RULE_REJECTED = "VENUE_RULE_REJECTED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_LIMIT_EXCEEDED = "ORDER_LIMIT_EXCEEDED"
    UNSUPPORTED_PLAN_ACTION = "UNSUPPORTED_PLAN_ACTION"
    INTERNAL_UNEXPECTED_ERROR = "INTERNAL_UNEXPECTED_ERROR"
    CANCEL_TARGET_NOT_FOUND = "CANCEL_TARGET_NOT_FOUND"
    ORDER_STATE_ERROR = "ORDER_STATE_ERROR"
    INVARIANT_VIOLATION = "INVARIANT_VIOLATION"
    RECONCILIATION_BLOCKED = "RECONCILIATION_BLOCKED"
    CLIENT_ORDER_ID_COLLISION = "CLIENT_ORDER_ID_COLLISION"
    POSITION_UNPROTECTED = "POSITION_UNPROTECTED"
