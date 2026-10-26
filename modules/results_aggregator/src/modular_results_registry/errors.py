from __future__ import annotations


class RegistryError(RuntimeError):
    """Base error for the results registry candidate."""


class ValidationError(RegistryError, ValueError):
    """A typed input contract is invalid."""


class IntegrityError(RegistryError):
    """A hash, reference, or registry invariant failed."""


class ConflictError(IntegrityError):
    """A deterministic identifier was presented with different content."""


class UnsupportedSchemaError(ValidationError):
    """A schema or definition version has no explicit adapter."""


class CompatibilityError(ValidationError):
    """Results cannot be compared under the requested compatibility contract."""


class NonComparableError(CompatibilityError):
    """Results failed the comparison compatibility gate."""


class ResourceLimitError(ValidationError):
    """An input exceeds a configured local resource limit."""


class RecoveryError(RegistryError):
    """Backup, restore, or rebuild validation failed."""


class UnsupportedPlatformError(RegistryError):
    """The runtime lacks the POSIX safety primitives required by this backend."""
