from __future__ import annotations


class TradingRunnerError(RuntimeError):
    """Base error for deterministic Trading Runner failures."""


class ConfigurationError(TradingRunnerError, ValueError):
    pass


class LifecycleTransitionError(TradingRunnerError):
    pass


class BootstrapError(TradingRunnerError):
    pass


class ReadinessError(BootstrapError):
    pass


class RecoveryError(BootstrapError):
    pass


class ReconciliationError(BootstrapError):
    pass


class PlanDispatchError(TradingRunnerError):
    pass


class PlanIdentityConflict(PlanDispatchError):
    pass


class KillSwitchActiveError(PlanDispatchError):
    pass


class KillSwitchResetError(TradingRunnerError):
    pass


class MarketDataError(TradingRunnerError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class MarketDataRejected(MarketDataError):
    pass


class MarketIdentityConflict(MarketDataRejected):
    pass


class CriticalQueueOverflow(MarketDataError):
    pass


class PipelineContractError(TradingRunnerError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class JournalIntegrityError(RecoveryError):
    pass


class CheckpointIntegrityError(RecoveryError):
    pass


class FactualEventRoutingError(TradingRunnerError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class FactualEventIdentityConflict(FactualEventRoutingError):
    pass


class FactualEventRecoveryRequired(FactualEventRoutingError):
    pass


class PaperBarExecutionError(TradingRunnerError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class PaperBarIdentityConflict(PaperBarExecutionError):
    pass


class PaperBarRecoveryRequired(PaperBarExecutionError):
    pass


class MarketCycleError(TradingRunnerError):
    def __init__(self, message: str, *, reason_code: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


class MarketCycleRecoveryRequired(MarketCycleError):
    pass
