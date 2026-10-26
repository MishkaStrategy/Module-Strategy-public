from enum import StrEnum


class PivotMethod(StrEnum):
    FRACTAL = "fractal"
    PERCENTAGE_ZIGZAG = "percentage_zigzag"
    ATR_ZIGZAG = "atr_zigzag"
    HYBRID_ADAPTIVE = "hybrid_adaptive"


class PivotKind(StrEnum):
    HIGH = "high"
    LOW = "low"


class PivotStatus(StrEnum):
    PROVISIONAL = "provisional"
    CONFIRMED = "confirmed"


class TiePolicy(StrEnum):
    EARLIEST = "earliest"
    LATEST = "latest"
    REJECT = "reject"


class PriceBasis(StrEnum):
    CLOSE = "close"
    HIGH_LOW = "high_low"


class WaveDirection(StrEnum):
    UP = "up"
    DOWN = "down"


class WaveClassification(StrEnum):
    IMPULSE = "impulse"
    CORRECTION = "correction"
    EXPANSION = "expansion"
    CONTRACTION = "contraction"
    EXHAUSTION = "exhaustion"
    RANGE_LEG = "range_leg"
    UNCLASSIFIED = "unclassified"


class SwingLabel(StrEnum):
    HIGHER_HIGH = "higher_high"
    HIGHER_LOW = "higher_low"
    LOWER_HIGH = "lower_high"
    LOWER_LOW = "lower_low"
    EQUAL_HIGH = "equal_high"
    EQUAL_LOW = "equal_low"
    RANGE = "range"
    UNDEFINED = "undefined"


class StructureState(StrEnum):
    BULLISH = "bullish_structure"
    BEARISH = "bearish_structure"
    RANGE = "range_structure"
    TRANSITION = "transition"
    POSSIBLE_REVERSAL = "possible_reversal"
    UNDEFINED = "undefined"


class HypothesisPattern(StrEnum):
    IMPULSE_12345 = "impulsive_1_2_3_4_5"
    CORRECTION_ABC = "corrective_a_b_c"
    POSSIBLE_EXTENSION = "possible_extension"
    TRUNCATED_FIFTH = "truncated_fifth"
    DIAGONAL_LIKE = "diagonal_like"
    COMPLEX_CORRECTION = "complex_correction"
    INCOMPLETE = "incomplete_hypothesis"


class HypothesisStatus(StrEnum):
    ACTIVE = "active"
    INCOMPLETE = "incomplete"
    INVALIDATED = "invalidated"


class LifecycleState(StrEnum):
    INSUFFICIENT_DATA = "insufficient_data"
    WARMING_UP = "warming_up"
    TRACKING_INITIAL_EXTREME = "tracking_initial_extreme"
    TRACKING_PROVISIONAL_PIVOT = "tracking_provisional_pivot"
    PIVOT_CONFIRMED = "pivot_confirmed"
    WAVE_LEG_CONFIRMED = "wave_leg_confirmed"
    STRUCTURE_UPDATED = "structure_updated"
    HYPOTHESES_UPDATED = "hypotheses_updated"
    GAP_BLOCKED = "gap_blocked"
    RECOVERY_REQUIRED = "recovery_required"
    RESET_REQUIRED = "reset_required"
    INVALID_STATE = "invalid_state"


class SkipReason(StrEnum):
    EMPTY_VIEW = "wave_analysis.empty_view"
    DUPLICATE_BAR = "wave_analysis.duplicate_bar"
    NO_COMPLETED_TARGET_BAR = "wave_analysis.no_completed_target_bar"
    NO_NEW_TARGET_BAR = "wave_analysis.no_new_target_bar"
    DATA_GAP = "wave_analysis.data_gap"
    RECOVERY_WARMUP = "wave_analysis.recovery_warmup"
    MODULE_NOT_READY = "wave_analysis.module_not_ready"
    NO_STATE_CHANGE = "wave_analysis.no_state_change"
