from enum import Enum


class TradeSide(str, Enum):
    BUY = "buy"
    SELL = "sell"


class RiskDecisionStatus(str, Enum):
    APPROVED = "approved"
    REJECTED = "rejected"


class RiskRejectReason(str, Enum):
    INVALID_REQUEST = "invalid_request"
    DUPLICATE_REQUEST = "duplicate_request"
    REQUEST_ID_CONFLICT = "request_id_conflict"
    STALE_REQUEST = "stale_request"
    FUTURE_REQUEST = "future_request"
    UNSUPPORTED_SCHEMA = "unsupported_schema"
    STREAM_CHANGED = "stream_changed"
    NON_POSITIVE_EQUITY = "non_positive_equity"
    INVALID_PRICE_GEOMETRY = "invalid_price_geometry"
    PRICE_NOT_ALIGNED = "price_not_aligned"
    RISK_FRACTION_OUT_OF_RANGE = "risk_fraction_out_of_range"
    NO_RISK_CAPACITY = "no_risk_capacity"
    MAX_POSITIONS_REACHED = "max_positions_reached"
    NO_NOTIONAL_CAPACITY = "no_notional_capacity"
    ZERO_QUANTITY_AFTER_ROUNDING = "zero_quantity_after_rounding"
    BELOW_MIN_QUANTITY = "below_min_quantity"
    ABOVE_MAX_QUANTITY = "above_max_quantity"
    BELOW_MIN_NOTIONAL = "below_min_notional"
    ABOVE_MAX_NOTIONAL = "above_max_notional"
    LEVERAGE_LIMIT = "leverage_limit"
    INVALID_POSITION_ADJUSTMENT = "invalid_position_adjustment"
    PROJECTED_POSITION_LIMIT = "projected_position_limit"
