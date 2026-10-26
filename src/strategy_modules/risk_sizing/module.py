"""Backward-compatible Module API import path."""

from modular_core import ModuleManifest

from .adapter import (
    FEATURE_DECISION,
    FEATURE_REQUEST,
    FEATURE_SCHEMA_VERSION,
    PARAMETER_SCHEMA,
    STATE_SCHEMA_VERSION,
    RiskSizingModule as _RiskSizingModule,
)


class RiskSizingModule(_RiskSizingModule):
    """Risk Sizing 1.2.0 with typed projected-position enforcement."""

    manifest = ModuleManifest(
        name=_RiskSizingModule.name,
        version="1.2.0",
        api_version="1.0",
        required_features=(FEATURE_REQUEST,),
        produced_features=(FEATURE_DECISION,),
        parameters_schema=PARAMETER_SCHEMA,
        description=(
            "Fixed-fractional risk, projected-position geometry, rounding and "
            "portfolio/notional constraints."
        ),
        state_schema_version=STATE_SCHEMA_VERSION,
        skip_until_ready=False,
    )


__all__ = [
    "FEATURE_DECISION",
    "FEATURE_REQUEST",
    "FEATURE_SCHEMA_VERSION",
    "PARAMETER_SCHEMA",
    "STATE_SCHEMA_VERSION",
    "RiskSizingModule",
]
