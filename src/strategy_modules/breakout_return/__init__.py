"""Breakout–Return strategy module."""

from .config import (
    BreakoutConfirmation,
    BreakoutReturnConfig,
    DirectionMode,
    RangeChangePolicy,
    ReferenceMode,
    ReturnConfirmation,
)
from .hardened import BreakoutReturnModule
from .models import (
    BreakoutDirection,
    BreakoutReturnDataError,
    BreakoutReturnError,
    BreakoutReturnPhase,
    BreakoutReturnReason,
    BreakoutReturnStateError,
    ReferenceRange,
)

__all__ = [
    "BreakoutConfirmation",
    "BreakoutDirection",
    "BreakoutReturnConfig",
    "BreakoutReturnDataError",
    "BreakoutReturnError",
    "BreakoutReturnModule",
    "BreakoutReturnPhase",
    "BreakoutReturnReason",
    "BreakoutReturnStateError",
    "DirectionMode",
    "RangeChangePolicy",
    "ReferenceMode",
    "ReferenceRange",
    "ReturnConfirmation",
]
