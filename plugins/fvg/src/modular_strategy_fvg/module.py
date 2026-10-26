from __future__ import annotations

from .module_feature_boundaries import _FvgFeatureBoundaryMixin
from .module_features import _FvgFeatureMixin
from .module_init import _FvgInitMixin
from .module_onbar import _FvgOnBarMixin
from .module_processing import _FvgProcessingMixin
from .module_restore import _FvgRestoreMixin
from .module_runtime import _FvgRuntimeMixin
from .module_shared import (
    ACTIVE_ZONE_KEY,
    CREATED_EVENT_KEY,
    EVALUATION_EVENT_KEY,
    INVALIDATED_EVENT_KEY,
    PRE_CANDIDATE_KEY,
    PRE_STATUS_KEY,
    ZONE_STATE_KEY,
    FvgDataConflictError,
    FvgDataOrderError,
    FvgReasonCode,
    FvgStateError,
    FvgSymbolScopeError,
    StrategyModule,
)
from .module_state import _FvgStateMixin
from .module_support import _FvgSupportMixin


class FvgModule(
    _FvgOnBarMixin,
    _FvgProcessingMixin,
    _FvgFeatureBoundaryMixin,
    _FvgRuntimeMixin,
    _FvgRestoreMixin,
    _FvgStateMixin,
    _FvgFeatureMixin,
    _FvgSupportMixin,
    _FvgInitMixin,
    StrategyModule,
):
    """Causal, deterministic Fair Value Gap producer for Module API v1."""

    pass


__all__ = [
    "ACTIVE_ZONE_KEY",
    "CREATED_EVENT_KEY",
    "EVALUATION_EVENT_KEY",
    "INVALIDATED_EVENT_KEY",
    "PRE_CANDIDATE_KEY",
    "PRE_STATUS_KEY",
    "ZONE_STATE_KEY",
    "FvgDataConflictError",
    "FvgDataOrderError",
    "FvgModule",
    "FvgReasonCode",
    "FvgStateError",
    "FvgSymbolScopeError",
]
