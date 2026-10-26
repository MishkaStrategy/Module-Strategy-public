"""Typed domain errors for Strategy Factory."""


class StrategyFactoryError(RuntimeError):
    """Base error for Strategy Factory operations."""


class ContractError(StrategyFactoryError, ValueError):
    """A versioned Strategy Factory contract is invalid."""


class DatasetRejectedError(StrategyFactoryError):
    """Dataset gate rejected the input and research must not continue."""


class CompatibilityError(StrategyFactoryError):
    """Registered modules cannot be composed safely."""


class BudgetExhaustedError(StrategyFactoryError):
    """The configured research budget has been exhausted."""


class CheckpointError(StrategyFactoryError):
    """A checkpoint cannot be safely read, written, or resumed."""


class IdempotencyConflictError(StrategyFactoryError):
    """An idempotency key was reused with different content."""


class ExternalComponentUnavailableError(StrategyFactoryError):
    """A required neighboring component is not available."""


class StageExecutionError(StrategyFactoryError):
    """A lifecycle stage failed and cannot be accepted as completed."""


class StageBlockedError(StrategyFactoryError):
    """A lifecycle stage is blocked by a retryable external dependency."""
