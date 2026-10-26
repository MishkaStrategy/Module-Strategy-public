from .config import SetupAdapterConfig, SourceSpec
from .contract import (
    CANONICAL_SETUP_SCHEMA_VERSION,
    build_canonical_setup_payload,
    validate_canonical_setup_payload,
)
from .enums import AdapterReason, SetupAdapterDataError, SetupAdapterStateError, SourceProfile
from .module import CanonicalSetupAdapterModule
from .outputs import REJECTION_FEATURE_KEY, SETUP_FEATURE_KEY

__all__ = [
    "CANONICAL_SETUP_SCHEMA_VERSION",
    "REJECTION_FEATURE_KEY",
    "SETUP_FEATURE_KEY",
    "AdapterReason",
    "CanonicalSetupAdapterModule",
    "SetupAdapterConfig",
    "SetupAdapterDataError",
    "SetupAdapterStateError",
    "SourceProfile",
    "SourceSpec",
    "build_canonical_setup_payload",
    "validate_canonical_setup_payload",
]

__version__ = "0.1.0"
