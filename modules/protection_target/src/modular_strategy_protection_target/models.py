from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from modular_core import Side

from .constants import FEATURE_SCHEMA_VERSION
from .decimal_utils import decimal_text


class ProtectionTargetReason(StrEnum):
    EMPTY_MARKET_VIEW = "protection_target.empty_market_view"
    NO_FEATURE_VIEW = "protection_target.no_feature_view"
    NO_ENTRY_PLAN = "protection_target.no_entry_plan"
    STALE_ENTRY_PLAN = "protection_target.stale_entry_plan"
    DUPLICATE_EVENT_IGNORED = "protection_target.duplicate_event_ignored"
    ENTRY_PLAN_ALREADY_PROCESSED = "protection_target.entry_plan_already_processed"
    DATA_GAP_SKIPPED = "protection_target.data_gap_skipped"
    INVALID_ENTRY_PLAN = "protection_target.invalid_entry_plan"
    MISSING_INVALIDATION = "protection_target.missing_invalidation"
    INVALID_INVALIDATION = "protection_target.invalid_invalidation"
    MISSING_STRUCTURAL_TARGET = "protection_target.missing_structural_target"
    INVALID_STRUCTURAL_TARGET = "protection_target.invalid_structural_target"
    STOP_DISTANCE_BELOW_MINIMUM = "protection_target.stop_distance_below_minimum"
    STOP_DISTANCE_ABOVE_MAXIMUM = "protection_target.stop_distance_above_maximum"
    REWARD_R_BELOW_MINIMUM = "protection_target.reward_r_below_minimum"
    REWARD_R_ABOVE_MAXIMUM = "protection_target.reward_r_above_maximum"
    INVALID_GEOMETRY = "protection_target.invalid_geometry"
    AMBIGUOUS_STRUCTURAL_FEATURE = "protection_target.ambiguous_structural_feature"
    INVALID_NUMERIC_OPERATION = "protection_target.invalid_numeric_operation"
    PLAN_CREATED = "protection_target.plan_created"


@dataclass(frozen=True, slots=True)
class ProtectionPlan:
    protection_plan_id: str
    entry_plan_id: str
    setup_id: str
    symbol: str
    timeframe: str
    side: Side
    entry_price: Decimal
    invalidation_price: Decimal
    stop_loss: Decimal
    take_profit: Decimal
    risk_distance: Decimal
    reward_distance: Decimal
    stop_distance_bps: Decimal
    reward_r: Decimal
    invalidation_mode: str
    target_mode: str
    created_at: datetime
    valid_until: datetime | None
    source_entry_feature_id: str | None
    source_invalidation_feature_id: str | None
    source_target_feature_id: str | None
    max_holding_seconds: int | None = None
    no_progress_rule: Mapping[str, object] | None = None

    def as_mapping(self) -> Mapping[str, object]:
        result: dict[str, object] = {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "protection_plan_id": self.protection_plan_id,
            "entry_plan_id": self.entry_plan_id,
            "setup_id": self.setup_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "side": self.side.value,
            "entry_price": decimal_text(self.entry_price),
            "invalidation_price": decimal_text(self.invalidation_price),
            "stop_loss": decimal_text(self.stop_loss),
            "take_profit": decimal_text(self.take_profit),
            "risk_distance": decimal_text(self.risk_distance),
            "reward_distance": decimal_text(self.reward_distance),
            "stop_distance_bps": decimal_text(self.stop_distance_bps),
            "reward_r": decimal_text(self.reward_r),
            "invalidation_mode": self.invalidation_mode,
            "target_mode": self.target_mode,
            "created_at": self.created_at.isoformat(),
            "valid_until": self.valid_until.isoformat() if self.valid_until is not None else None,
            "source_entry_feature_id": self.source_entry_feature_id,
            "source_invalidation_feature_id": self.source_invalidation_feature_id,
            "source_target_feature_id": self.source_target_feature_id,
        }
        if self.max_holding_seconds is not None:
            result["max_holding_seconds"] = self.max_holding_seconds
        if self.no_progress_rule is not None:
            result["no_progress_rule"] = dict(self.no_progress_rule)
        return result


@dataclass(frozen=True, slots=True)
class ProtectionRejection:
    process_key: str
    entry_plan_id: str | None
    setup_id: str | None
    reason: ProtectionTargetReason
    detail: str
    created_at: datetime
    source_entry_feature_id: str | None

    def as_mapping(self) -> Mapping[str, object]:
        return {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "process_key": self.process_key,
            "entry_plan_id": self.entry_plan_id,
            "setup_id": self.setup_id,
            "reason": self.reason.value,
            "detail": self.detail,
            "created_at": self.created_at.isoformat(),
            "source_entry_feature_id": self.source_entry_feature_id,
        }
