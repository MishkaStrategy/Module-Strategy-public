from __future__ import annotations

from enum import StrEnum


class SourceProfile(StrEnum):
    BREAKOUT_RETURN_V1 = "breakout_return_v1"
    CANONICAL_SETUP_V1 = "canonical_setup_v1"
    FVG_RT_FINAL_15M_V1 = "fvg_rt_final_15m_v1"
    FVG_RT_FROZEN_1H_V1 = "fvg_rt_frozen_1h_v1"
    FVG_RT_FROZEN_4H_V1 = "fvg_rt_frozen_4h_v1"
    LIQUIDITY_SWEEP_V1 = "liquidity_sweep_v1"


class AdapterReason(StrEnum):
    DUPLICATE_EVENT = "setup_adapter.duplicate_event"
    DATA_GAP = "setup_adapter.data_gap"
    FEATURE_VIEW_UNAVAILABLE = "setup_adapter.feature_view_unavailable"
    DUPLICATE_SOURCE = "setup_adapter.duplicate_source"
    SOURCE_FEATURE_CONFLICT = "setup_adapter.source_feature_conflict"
    INVALID_SOURCE_PAYLOAD = "setup_adapter.invalid_source_payload"
    UNSUPPORTED_SOURCE_SCHEMA = "setup_adapter.unsupported_source_schema"
    SYMBOL_MISMATCH = "setup_adapter.symbol_mismatch"
    TIMEFRAME_MISMATCH = "setup_adapter.timeframe_mismatch"
    FEATURE_CAUSALITY_VIOLATION = "setup_adapter.feature_causality_violation"
    INVALID_PRICE_GEOMETRY = "setup_adapter.invalid_price_geometry"


class SetupAdapterDataError(ValueError):
    pass


class SetupAdapterStateError(ValueError):
    pass
