from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping


def canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("payload contains non-finite float")
        return {"$float": repr(value)}
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("payload contains non-finite Decimal")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("payload datetime must be timezone-aware")
        return {"$datetime": value.isoformat()}
    if isinstance(value, Enum):
        return {"$enum": f"{type(value).__module__}.{type(value).__qualname__}:{value.value}"}
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("payload mapping keys must be strings")
            result[key] = canonicalize(item)
        return result
    if isinstance(value, (list, tuple)):
        return [canonicalize(item) for item in value]
    raise ValueError(f"payload contains unsupported value type: {type(value).__name__}")


def fingerprint(value: Any) -> str:
    canonical = json.dumps(
        canonicalize(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def manifest_number(value: Decimal, name: str) -> float:
    result = float(value)
    if not math.isfinite(result) or Decimal(str(result)) != value:
        raise ValueError(f"{name} precision cannot be represented by Module API numeric parameters")
    return result
