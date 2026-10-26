from .models_base import (
    FvgDirection,
    FvgEvaluationStatus,
    FvgFilterDecision,
    FvgLifecycleStatus,
    FvgZone,
    _FILTER_TRACE_ORDER,
    _finite_float,
    _require_canonical_mapping_keys,
    _require_canonical_symbol,
)
from .models_filters import FvgEvaluation, FvgFilterTrace, FvgMetrics
from .models_pre import (
    FvgPreCandidate as _FvgPreCandidateBase,
    FvgPreEvaluation,
    FvgPreStatus,
)


class FvgPreCandidate(_FvgPreCandidateBase):
    """Schema-4 provisional candidate with runtime-only invariants."""

    def __post_init__(self) -> None:
        super().__post_init__()
        if self.metrics.distance_from_reference is None:
            raise ValueError("pre-FVG candidate requires distance_from_reference")


__all__ = [
    "FvgDirection",
    "FvgEvaluationStatus",
    "FvgFilterDecision",
    "FvgLifecycleStatus",
    "FvgZone",
    "FvgFilterTrace",
    "FvgMetrics",
    "FvgEvaluation",
    "FvgPreStatus",
    "FvgPreCandidate",
    "FvgPreEvaluation",
    "_FILTER_TRACE_ORDER",
    "_finite_float",
    "_require_canonical_mapping_keys",
    "_require_canonical_symbol",
]
