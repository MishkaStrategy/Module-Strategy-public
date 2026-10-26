"""Canonical serialization compatible with Strategy Factory contract hashing."""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

_MAX_DEPTH = 96


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented deterministically."""


def canonical_value(value: Any, *, depth: int = 0) -> Any:
    """Return the Strategy Factory-compatible canonical JSON value.

    Audit evidence is validated separately and may impose stricter semantic
    constraints than the transport codec. Keeping this representation byte-
    compatible with Strategy Factory is required for ``decision_hash`` replay.
    """

    if depth > _MAX_DEPTH:
        raise CanonicalizationError("canonical value exceeds maximum nesting depth")
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: canonical_value(getattr(value, field.name), depth=depth + 1)
            for field in fields(value)
        }
    if isinstance(value, Enum):
        return canonical_value(value.value, depth=depth + 1)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalizationError("datetime values must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError("Decimal values must be finite")
        return {"$decimal": format(value, "f")}
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise CanonicalizationError("canonical mapping keys must be non-empty strings")
            if key in normalized:
                raise CanonicalizationError("canonical mapping contains a duplicate key")
            normalized[key] = canonical_value(item, depth=depth + 1)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, (set, frozenset)):
        items = [canonical_value(item, depth=depth + 1) for item in value]
        return sorted(items, key=canonical_json_bytes)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [canonical_value(item, depth=depth + 1) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("floating-point values must be finite")
        return value
    if value is None or isinstance(value, (str, int, bool)):
        return value
    raise CanonicalizationError(f"unsupported canonical value: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def strict_json_loads(payload: str | bytes) -> Any:
    """Decode JSON while rejecting duplicate keys and non-finite constants."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise CanonicalizationError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(value: str) -> Any:
        raise CanonicalizationError(f"invalid JSON constant: {value}")

    try:
        return json.loads(
            payload,
            object_pairs_hook=object_pairs,
            parse_constant=reject_constant,
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CanonicalizationError("invalid JSON document") from exc
