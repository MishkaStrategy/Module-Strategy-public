from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Mapping

MAX_CANONICAL_DEPTH = 64
MAX_CANONICAL_CONTAINER_ITEMS = 1_000_000
MAX_CANONICAL_SCALAR_CHARS = 16 * 1024 * 1024
MAX_CANONICAL_INTEGER_BITS = 13_000


def utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError(f"{name} must be datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return value.astimezone(timezone.utc)


def decimal(
    value: object,
    name: str,
    *,
    positive: bool = False,
) -> Decimal:
    if isinstance(value, bool) or not isinstance(
        value,
        (Decimal, str, int, float),
    ):
        raise TypeError(f"{name} must be a decimal-compatible scalar")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal number") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def canonical(value: Any) -> Any:
    return _canonical(value, depth=0, active_containers=set())


def _canonical(
    value: Any,
    *,
    depth: int,
    active_containers: set[int],
) -> Any:
    if depth > MAX_CANONICAL_DEPTH:
        raise ValueError("canonical value exceeds maximum nesting depth")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal is not canonicalizable")
        decimal_tuple = value.as_tuple()
        digits = len(decimal_tuple.digits)
        exponent = decimal_tuple.exponent
        if not isinstance(exponent, int):
            raise ValueError("invalid decimal exponent")
        estimated_chars = digits + max(abs(exponent), 1) + 2
        if estimated_chars > MAX_CANONICAL_SCALAR_CHARS:
            raise ValueError("canonical decimal representation is too large")
        text = format(value, "f")
        if len(text) > MAX_CANONICAL_SCALAR_CHARS:
            raise ValueError("canonical decimal representation is too large")
        return text
    if isinstance(value, datetime):
        return utc(value, "datetime").isoformat().replace("+00:00", "Z")
    if isinstance(value, Enum):
        return _canonical(
            value.value,
            depth=depth + 1,
            active_containers=active_containers,
        )
    if isinstance(value, Mapping):
        if len(value) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise ValueError("canonical mapping exceeds item limit")
        identity = id(value)
        if identity in active_containers:
            raise ValueError("canonical value contains a cycle")
        if any(not isinstance(key, str) for key in value):
            raise TypeError("canonical mapping keys must be strings")
        for key in value:
            if len(key) > MAX_CANONICAL_SCALAR_CHARS:
                raise ValueError("canonical mapping key is too large")
        active_containers.add(identity)
        try:
            return {
                key: _canonical(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for key, item in sorted(value.items())
            }
        finally:
            active_containers.remove(identity)
    if isinstance(value, (tuple, list)):
        if len(value) > MAX_CANONICAL_CONTAINER_ITEMS:
            raise ValueError("canonical sequence exceeds item limit")
        identity = id(value)
        if identity in active_containers:
            raise ValueError("canonical value contains a cycle")
        active_containers.add(identity)
        try:
            return [
                _canonical(
                    item,
                    depth=depth + 1,
                    active_containers=active_containers,
                )
                for item in value
            ]
        finally:
            active_containers.remove(identity)
    if isinstance(value, str):
        if len(value) > MAX_CANONICAL_SCALAR_CHARS:
            raise ValueError("canonical string is too large")
        return value
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, int):
        if value.bit_length() > MAX_CANONICAL_INTEGER_BITS:
            raise ValueError("canonical integer representation is too large")
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not canonicalizable")
        return repr(value)
    raise TypeError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
