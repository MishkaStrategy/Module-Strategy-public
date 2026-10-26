"""Domain exceptions for the Fibonacci module."""


class FibonacciError(Exception):
    """Base exception for Fibonacci module failures."""


class DataValidationError(FibonacciError):
    """Raised when market data violates required invariants."""


class ConfigurationError(FibonacciError):
    """Raised when module configuration is invalid."""


class AnnotationError(FibonacciError):
    """Raised for invalid or conflicting annotation writes."""
