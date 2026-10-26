from __future__ import annotations

from datetime import datetime
from decimal import Decimal, DecimalException
from enum import Enum
import math
from types import MappingProxyType
from typing import Any, Mapping

ZERO = Decimal("0")
ONE = Decimal("1")
MAX_DECIMAL_DIGITS = 1000
MAX_DECIMAL_ADJUSTED = 1000


def decimal_work_precision(*values: Decimal) -> int:
    """Return deterministic context precision for bounded financial arithmetic."""
    precision = 50
    for value in values:
        _, digits, exponent = value.as_tuple()
        exponent_value = exponent if isinstance(exponent, int) else 0
        precision = max(precision, len(digits) + abs(exponent_value) + 16)
    return min(4096, precision)


def freeze_value(value: Any) -> Any:
    """Deep-freeze supported JSON-like metadata and reject unsafe values."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("metadata floats must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("metadata Decimals must be finite")
        return value
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("metadata datetimes must be timezone-aware")
        return value
    if isinstance(value, Enum):
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("metadata mapping keys must be strings")
            frozen[key] = freeze_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(freeze_value(item) for item in value)
    raise ValueError(f"metadata contains unsupported type: {type(value).__name__}")


def decimal_from(value: Decimal | int | float | str, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be numeric")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (DecimalException, ValueError, TypeError) as exc:
        raise ValueError(f"{field_name} must be a valid decimal") from exc
    if not result.is_finite():
        raise ValueError(f"{field_name} must be finite")
    if (
        len(result.as_tuple().digits) > MAX_DECIMAL_DIGITS
        or abs(result.adjusted()) > MAX_DECIMAL_ADJUSTED
    ):
        raise ValueError(f"{field_name} magnitude or precision is unsupported")
    return result
