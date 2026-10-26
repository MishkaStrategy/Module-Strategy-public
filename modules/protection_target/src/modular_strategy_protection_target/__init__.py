from __future__ import annotations

from .config import (
    InvalidationMode,
    ProtectionTargetConfig,
    StructuralTargetSource,
    TargetMode,
)
from .constants import (
    ENTRY_PLAN_FEATURE_KEY,
    FEATURE_SCHEMA_VERSION,
    PROTECTION_PLAN_FEATURE_KEY,
    PROTECTION_REJECTION_FEATURE_KEY,
)
from .contracts import EntryPlanContract
from .exceptions import (
    ProtectionTargetConfigurationError,
    ProtectionTargetContractError,
    ProtectionTargetDataError,
    ProtectionTargetError,
    ProtectionTargetStateError,
)
from .models import ProtectionPlan, ProtectionRejection, ProtectionTargetReason
from .module import ProtectionTargetModule

__version__ = "1.1.1"

__all__ = [
    "__version__",
    "ENTRY_PLAN_FEATURE_KEY",
    "FEATURE_SCHEMA_VERSION",
    "PROTECTION_PLAN_FEATURE_KEY",
    "PROTECTION_REJECTION_FEATURE_KEY",
    "EntryPlanContract",
    "InvalidationMode",
    "ProtectionPlan",
    "ProtectionRejection",
    "ProtectionTargetConfig",
    "ProtectionTargetConfigurationError",
    "ProtectionTargetContractError",
    "ProtectionTargetDataError",
    "ProtectionTargetError",
    "ProtectionTargetModule",
    "ProtectionTargetReason",
    "ProtectionTargetStateError",
    "StructuralTargetSource",
    "TargetMode",
]
