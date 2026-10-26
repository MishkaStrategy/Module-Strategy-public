class WaveAnalysisError(Exception):
    """Base error for the Wave Structure Analysis module."""


class WaveAnalysisConfigurationError(WaveAnalysisError, ValueError):
    """Raised when module configuration is invalid."""


class WaveAnalysisDataError(WaveAnalysisError, ValueError):
    """Raised when market data violates point-in-time invariants."""


class WaveAnalysisStateError(WaveAnalysisError, ValueError):
    """Raised when checkpoint state is invalid or incompatible."""
