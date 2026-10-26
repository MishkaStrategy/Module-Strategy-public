class MarketRegimeError(Exception):
    """Base exception for the Market Regime module."""


class MarketRegimeConfigurationError(MarketRegimeError, ValueError):
    """Raised when the module configuration is invalid."""


class MarketRegimeDataError(MarketRegimeError, ValueError):
    """Raised when input data would corrupt deterministic state."""


class MarketRegimeStateError(MarketRegimeError, ValueError):
    """Raised when a checkpoint cannot be validated or restored."""
