from typing import Any

FEATURE_SCHEMA_VERSION = "1"
CONFIRMED_PIVOTS_FEATURE = "wave_analysis.confirmed_pivots"
PROVISIONAL_PIVOT_FEATURE = "wave_analysis.provisional_pivot"
WAVE_LEGS_FEATURE = "wave_analysis.wave_legs"
CURRENT_WAVE_FEATURE = "wave_analysis.current_wave"
STRUCTURE_FEATURE = "wave_analysis.structure"
IMPULSE_SCORE_FEATURE = "wave_analysis.impulse_score"
CORRECTION_SCORE_FEATURE = "wave_analysis.correction_score"
RETRACEMENT_FEATURE = "wave_analysis.retracement"
EXTENSION_FEATURE = "wave_analysis.extension"
INVALIDATION_FEATURE = "wave_analysis.invalidation"
HYPOTHESES_FEATURE = "wave_analysis.hypotheses"
PRIMARY_HYPOTHESIS_FEATURE = "wave_analysis.primary_hypothesis"
MTF_CONTEXT_FEATURE = "wave_analysis.mtf_context"
CONFIDENCE_FEATURE = "wave_analysis.confidence"
PRODUCED_FEATURES = (
    CONFIRMED_PIVOTS_FEATURE,
    PROVISIONAL_PIVOT_FEATURE,
    WAVE_LEGS_FEATURE,
    CURRENT_WAVE_FEATURE,
    STRUCTURE_FEATURE,
    IMPULSE_SCORE_FEATURE,
    CORRECTION_SCORE_FEATURE,
    RETRACEMENT_FEATURE,
    EXTENSION_FEATURE,
    INVALIDATION_FEATURE,
    HYPOTHESES_FEATURE,
    PRIMARY_HYPOTHESIS_FEATURE,
    MTF_CONTEXT_FEATURE,
    CONFIDENCE_FEATURE,
)
type FeatureValue = dict[str, Any]
