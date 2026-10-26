from __future__ import annotations

from .errors import IntegrityError, RegistryError


def strict_sqlite_integer(
    value: object,
    field: str,
    *,
    minimum: int | None = None,
) -> int:
    if type(value) is not int:
        raise IntegrityError(f"{field} must be stored as a SQLite INTEGER")
    if minimum is not None and value < minimum:
        raise IntegrityError(f"{field} must be at least {minimum}")
    return value


def strict_schema_version_text(value: object) -> int:
    if type(value) is not str or not value or not value.isascii() or not value.isdecimal():
        raise RegistryError("registry schema version must be canonical decimal TEXT")
    version = int(value)
    if str(version) != value:
        raise RegistryError("registry schema version must be canonical decimal TEXT")
    return version
