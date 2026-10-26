from __future__ import annotations

from .enums import ReasonCode


class TradePlanRouterError(Exception):
    """Base class for unexpected Router failures."""


class RouterInputError(ValueError):
    def __init__(self, message: str, reason: ReasonCode = ReasonCode.INVALID_INPUT) -> None:
        super().__init__(message)
        self.reason = reason


class RouterConfigurationError(ValueError):
    pass


class RouterSerializationError(TradePlanRouterError, ValueError):
    """Raised when an external serialized Router payload is unsafe or invalid."""


class DownstreamContractError(TradePlanRouterError, ValueError):
    """Raised when a Router plan cannot be handed downstream without mutation."""


class PlanInvariantError(ValueError):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.reason = ReasonCode.PLAN_INVARIANT_FAILED
