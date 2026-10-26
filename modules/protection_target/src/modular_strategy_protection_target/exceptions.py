from __future__ import annotations

from .models import ProtectionTargetReason


class ProtectionTargetError(RuntimeError):
    """Base error for the Protection & Target module."""


class ProtectionTargetConfigurationError(ProtectionTargetError, ValueError):
    """Raised when module configuration is invalid."""


class ProtectionTargetContractError(ProtectionTargetError, ValueError):
    """Raised when an upstream feature violates the published contract."""

    def __init__(
        self,
        message: str,
        reason: ProtectionTargetReason = ProtectionTargetReason.INVALID_ENTRY_PLAN,
    ) -> None:
        super().__init__(message)
        self.reason = reason


class ProtectionTargetDataError(ProtectionTargetError, ValueError):
    """Raised when the market or feature stream is contradictory."""


class ProtectionTargetStateError(ProtectionTargetError, ValueError):
    """Raised when persisted state is malformed or incompatible."""
