from __future__ import annotations

from enum import StrEnum

from modular_core import OrderType


class EntryKind(StrEnum):
    MARKET = "market"
    LIMIT_CONFIRMATION = "limit_confirmation"
    LIMIT_RETRACEMENT = "limit_retracement"
    LIMIT_EXPLICIT = "limit_explicit"
    STOP_MARKET = "stop_market"
    STOP_LIMIT = "stop_limit"

    @property
    def order_type(self) -> OrderType:
        return {
            EntryKind.MARKET: OrderType.MARKET,
            EntryKind.LIMIT_CONFIRMATION: OrderType.LIMIT,
            EntryKind.LIMIT_RETRACEMENT: OrderType.LIMIT,
            EntryKind.LIMIT_EXPLICIT: OrderType.LIMIT,
            EntryKind.STOP_MARKET: OrderType.STOP_MARKET,
            EntryKind.STOP_LIMIT: OrderType.STOP_LIMIT,
        }[self]


class EntryRejectionReason(StrEnum):
    FEATURE_VIEW_UNAVAILABLE = "entry_model.feature_view_unavailable"
    DATA_GAP = "entry_model.data_gap"
    DUPLICATE_EVENT = "entry_model.duplicate_event"
    OUT_OF_ORDER_EVENT = "entry_model.out_of_order_event"
    CONFLICTING_EVENT = "entry_model.conflicting_event"
    INVALID_SETUP_PAYLOAD = "entry_model.invalid_setup_payload"
    UNSUPPORTED_SETUP_SCHEMA = "entry_model.unsupported_setup_schema"
    SETUP_NOT_CONFIRMED = "entry_model.setup_not_confirmed"
    SYMBOL_MISMATCH = "entry_model.symbol_mismatch"
    FEATURE_CAUSALITY_VIOLATION = "entry_model.feature_causality_violation"
    FUTURE_SETUP = "entry_model.future_setup"
    STALE_SETUP = "entry_model.stale_setup"
    DUPLICATE_SETUP = "entry_model.duplicate_setup"
    SOURCE_FEATURE_CONFLICT = "entry_model.source_feature_conflict"
    SETUP_REVISION_CONFLICT = "entry_model.setup_revision_conflict"
    ENTRY_KIND_NOT_ALLOWED = "entry_model.entry_kind_not_allowed"
    REQUIRED_PRICE_MISSING = "entry_model.required_price_missing"
    INVALID_PRICE_GEOMETRY = "entry_model.invalid_price_geometry"
    EXPIRED_AT_CREATION = "entry_model.expired_at_creation"
    INVALID_TIMEFRAME = "entry_model.invalid_timeframe"
    INTERNAL_ERROR = "entry_model.internal_error"


class SetupPayloadError(ValueError):
    """A setup feature cannot be parsed into the canonical contract."""


class EntryModelDataError(ValueError):
    """The input stream violates deterministic ordering or identity rules."""
