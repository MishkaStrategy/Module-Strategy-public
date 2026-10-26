from __future__ import annotations

from .enums import PaperReasonCode


class PaperExecutionError(RuntimeError):
    def __init__(
        self,
        reason_code: PaperReasonCode,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.retryable = retryable


class LiveExecutionNotSupported(PaperExecutionError):
    def __init__(self) -> None:
        super().__init__(
            PaperReasonCode.LIVE_EXECUTION_NOT_SUPPORTED,
            "live execution is not supported by this module",
        )


class PlanValidationError(PaperExecutionError):
    pass


class VenueRuleRejection(PaperExecutionError):
    pass


class OrderStateError(PaperExecutionError):
    pass


class ReconciliationBlocked(PaperExecutionError):
    pass
