from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, DecimalException, InvalidOperation
from enum import Enum
from functools import lru_cache
from types import MappingProxyType
from typing import Mapping, Sequence, cast

from modular_core import (
    Candle,
    Feature,
    FeatureDelete,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)

from .analysis import utc_session
from .config import (
    FvgConfig,
    FvgDirectionFilter,
    FvgFilterTarget,
    FvgGapSizeMode,
    FvgInvalidationBasis,
    FvgInvalidationLevel,
    FvgMissingDataPolicy,
    FvgOrderViolationPolicy,
    FvgPriceFilterMode,
    FvgPriceMatchPolicy,
    FvgVolumeFilterMode,
)
from .decimal_utils import (
    canonical_decimal_text,
    deterministic_decimal,
    parse_canonical_decimal_text,
)
from .detector import (
    CandleLike,
    _candle_fingerprint_validated,
    _detect_zone_validated,
    _optional_candle_attribute,
    _required_candle_attribute,
    _validate_candle,
    _validate_candle_clock,
    as_decimal,
    candle_fingerprint,
    make_zone_id,
)
from .evaluator import evaluate_fvg_window
from .filters import (
    evaluate_gap_size_filter,
    evaluate_price_filter,
    evaluate_volume_filter,
)
from .lifecycle import (
    FvgZoneLifecycle,
    _observe_lifecycle_validated,
    initial_lifecycle,
    terminal_lifecycle,
)
from .models import (
    FvgDirection,
    FvgEvaluation,
    FvgEvaluationStatus,
    FvgFilterDecision,
    FvgFilterTrace,
    FvgLifecycleStatus,
    FvgPreCandidate,
    FvgPreEvaluation,
    FvgPreStatus,
    FvgZone,
)
from .pre_fvg import evaluate_pre_fvg, make_pre_candidate_id
from .timeframes import (
    as_utc,
    is_period_aligned,
    parse_canonical_utc_datetime,
    period_close,
    period_open,
    timeframe_seconds,
)

ACTIVE_ZONE_KEY = "fvg.active_zone"
CREATED_EVENT_KEY = "fvg.zone_created"
INVALIDATED_EVENT_KEY = "fvg.zone_invalidated"
EVALUATION_EVENT_KEY = "fvg.evaluation"
PRE_CANDIDATE_KEY = "fvg.pre_candidate"
PRE_STATUS_KEY = "fvg.pre_status"
ZONE_STATE_KEY = "fvg.zone_state"
STATE_SCHEMA_VERSION = "4"
PREVIOUS_STATE_SCHEMA_VERSION = "3"
OLDER_STATE_SCHEMA_VERSION = "2"
LEGACY_STATE_SCHEMA_VERSION = "1"
MODULE_VERSION = "0.3.0"
PREVIOUS_MODULE_VERSION = "0.2.1"
OLDER_MODULE_VERSION = "0.2.0"
LEGACY_MODULE_VERSION = "0.1.0"
_RECENT_FINGERPRINT_LIMIT = 2
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_BarSignature = tuple[object, ...]
_HistoryReference = tuple[datetime, CandleLike]


@lru_cache(maxsize=32_768)
def _json_string_size(value: str) -> int:
    return len(json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode("utf-8"))


def _iso_datetime_string_size(value: datetime) -> int:
    return 34 if value.microsecond else 27


def _json_int_size(value: int) -> int:
    return len(str(value))


def _json_decimal_string_size(value: Decimal) -> int:
    return len(str(value)) + 2


def _json_array_size(row_sizes: Sequence[int]) -> int:
    return 2 if not row_sizes else 1 + sum(row_sizes) + len(row_sizes)


def _object_overhead(keys: Sequence[str]) -> int:
    return 2 + max(0, len(keys) - 1) + sum(_json_string_size(key) + 1 for key in keys)


_ZONE_ROW_OVERHEAD = _object_overhead(
    (
        "zone_id",
        "symbol",
        "timeframe",
        "direction",
        "lower",
        "upper",
        "created_at",
        "source_a_open_time",
        "source_b_open_time",
        "source_c_open_time",
    )
)
_LIFECYCLE_ROW_OVERHEAD = _object_overhead(
    (
        "zone_id",
        "status",
        "detected_at",
        "last_transition_at",
        "last_observed_at",
        "first_touch_at",
        "last_touch_at",
        "contact_count",
        "fill_percentage",
        "maximum_penetration",
        "age_bars",
    )
)
_HISTORY_ROW_OVERHEAD = _object_overhead(("close_time", "bar_fingerprint"))
_CURSOR_ROW_OVERHEAD = _object_overhead(
    (
        "symbol",
        "timeframe",
        "close_time",
        "bar_fingerprint",
        "history_fingerprints",
        "barrier_close_time",
    )
)
_PRE_EVALUATED_ROW_OVERHEAD = _object_overhead(("symbol", "timeframe", "source_c_open_time"))
_TOP_STATE_OVERHEAD = _object_overhead(
    (
        "state_schema_version",
        "module_version",
        "config_fingerprint",
        "bound_symbol",
        "zone_lifecycle",
        "pre_evaluated",
        "pre_candidates",
        "last_processed",
        "zones",
    )
)


class FvgReasonCode(str, Enum):
    EMPTY_BASE_BARS = "fvg.empty_base_bars"
    DATA_GAP_RESET = "fvg.data_gap_reset"
    DATA_GAP_DELETE = "fvg_data_gap_reset"
    ORDER_VIOLATION_DELETE = "fvg_order_violation_reset"
    MITIGATED_DELETE = "fvg_mitigated"
    CAPACITY_EVICTED_DELETE = "fvg_capacity_evicted"
    EXPIRED_DELETE = "fvg_expired"
    PRE_CANDIDATE_RESOLVED_DELETE = "fvg_pre_candidate_resolved"
    PRE_CANDIDATE_GAP_DELETE = "fvg_pre_candidate_gap_reset"
    PRE_CANDIDATE_ORDER_DELETE = "fvg_pre_candidate_order_reset"


class FvgDataOrderError(ValueError):
    pass


class FvgDataConflictError(ValueError):
    pass


class FvgSymbolScopeError(ValueError):
    pass


class FvgStateError(ValueError):
    pass


def _thaw_checkpoint_payload(
    value: object,
    *,
    depth: int = 0,
    active_containers: set[int] | None = None,
) -> object:
    if depth > 64:
        raise FvgStateError("FVG checkpoint container nesting exceeds the supported limit")
    if active_containers is None:
        active_containers = set()
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in active_containers:
            raise FvgStateError("FVG checkpoint contains a cyclic mapping")
        if any(type(key) is not str for key in value):
            raise FvgStateError("FVG checkpoint mapping keys must be canonical strings")
        active_containers.add(identity)
        try:
            return {
                key: _thaw_checkpoint_payload(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for key, item in value.items()
            }
        finally:
            active_containers.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active_containers:
            raise FvgStateError("FVG checkpoint contains a cyclic sequence")
        active_containers.add(identity)
        try:
            return [
                _thaw_checkpoint_payload(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for item in value
            ]
        finally:
            active_containers.remove(identity)
    return value


__all__ = [
    "hashlib",
    "json",
    "re",
    "defaultdict",
    "datetime",
    "timedelta",
    "timezone",
    "Decimal",
    "DecimalException",
    "InvalidOperation",
    "Enum",
    "lru_cache",
    "MappingProxyType",
    "Mapping",
    "Sequence",
    "cast",
    "Candle",
    "Feature",
    "FeatureDelete",
    "MarketView",
    "ModuleDecision",
    "ModuleManifest",
    "StrategyModule",
    "utc_session",
    "FvgConfig",
    "FvgDirectionFilter",
    "FvgFilterTarget",
    "FvgGapSizeMode",
    "FvgInvalidationBasis",
    "FvgInvalidationLevel",
    "FvgMissingDataPolicy",
    "FvgOrderViolationPolicy",
    "FvgPriceFilterMode",
    "FvgPriceMatchPolicy",
    "FvgFilterTarget",
    "FvgVolumeFilterMode",
    "FvgMissingDataPolicy",
    "canonical_decimal_text",
    "deterministic_decimal",
    "parse_canonical_decimal_text",
    "CandleLike",
    "_candle_fingerprint_validated",
    "_detect_zone_validated",
    "_optional_candle_attribute",
    "_required_candle_attribute",
    "_validate_candle",
    "_validate_candle_clock",
    "as_decimal",
    "candle_fingerprint",
    "make_zone_id",
    "evaluate_fvg_window",
    "evaluate_gap_size_filter",
    "evaluate_price_filter",
    "evaluate_volume_filter",
    "FvgZoneLifecycle",
    "_observe_lifecycle_validated",
    "initial_lifecycle",
    "terminal_lifecycle",
    "FvgDirection",
    "FvgEvaluation",
    "FvgEvaluationStatus",
    "FvgFilterDecision",
    "FvgFilterTrace",
    "FvgLifecycleStatus",
    "FvgPreCandidate",
    "FvgPreEvaluation",
    "FvgPreStatus",
    "FvgZone",
    "evaluate_pre_fvg",
    "make_pre_candidate_id",
    "as_utc",
    "is_period_aligned",
    "parse_canonical_utc_datetime",
    "period_close",
    "period_open",
    "timeframe_seconds",
    "ACTIVE_ZONE_KEY",
    "CREATED_EVENT_KEY",
    "INVALIDATED_EVENT_KEY",
    "EVALUATION_EVENT_KEY",
    "PRE_CANDIDATE_KEY",
    "PRE_STATUS_KEY",
    "ZONE_STATE_KEY",
    "STATE_SCHEMA_VERSION",
    "PREVIOUS_STATE_SCHEMA_VERSION",
    "OLDER_STATE_SCHEMA_VERSION",
    "LEGACY_STATE_SCHEMA_VERSION",
    "MODULE_VERSION",
    "PREVIOUS_MODULE_VERSION",
    "OLDER_MODULE_VERSION",
    "LEGACY_MODULE_VERSION",
    "_RECENT_FINGERPRINT_LIMIT",
    "_FINGERPRINT_RE",
    "_BarSignature",
    "_HistoryReference",
    "_json_string_size",
    "_iso_datetime_string_size",
    "_json_int_size",
    "_json_decimal_string_size",
    "_json_array_size",
    "_object_overhead",
    "_ZONE_ROW_OVERHEAD",
    "_LIFECYCLE_ROW_OVERHEAD",
    "_HISTORY_ROW_OVERHEAD",
    "_CURSOR_ROW_OVERHEAD",
    "_PRE_EVALUATED_ROW_OVERHEAD",
    "_TOP_STATE_OVERHEAD",
    "FvgReasonCode",
    "FvgDataOrderError",
    "FvgDataConflictError",
    "FvgSymbolScopeError",
    "FvgStateError",
    "_thaw_checkpoint_payload",
]
