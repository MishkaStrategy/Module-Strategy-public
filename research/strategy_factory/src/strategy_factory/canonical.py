"""Canonical serialization and content-addressed identity helpers."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .errors import ContractError

_MAX_DEPTH = 96


def _normalize(value: Any, *, depth: int = 0) -> Any:
    if depth > _MAX_DEPTH:
        raise ContractError("canonical value exceeds maximum nesting depth")
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _normalize(getattr(value, field.name), depth=depth + 1)
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Enum):
        return _normalize(value.value, depth=depth + 1)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ContractError("datetime values must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ContractError("Decimal values must be finite")
        return {"$decimal": format(value, "f")}
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise ContractError("canonical mapping keys must be non-empty strings")
            if key in normalized:
                raise ContractError("canonical mapping contains a duplicate key")
            normalized[key] = _normalize(item, depth=depth + 1)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, set | frozenset):
        items = [_normalize(item, depth=depth + 1) for item in value]
        return sorted(items, key=lambda item: canonical_json_bytes(item))
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [_normalize(item, depth=depth + 1) for item in value]
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractError("floating-point values must be finite")
        return value
    if value is None or isinstance(value, str | int | bool):
        return value
    raise ContractError(f"unsupported canonical value type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    """Return strict deterministic UTF-8 JSON bytes for a supported value."""

    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def canonical_sha256(value: Any) -> str:
    """Return a lowercase SHA-256 digest over canonical JSON."""

    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def freeze(value: Any) -> Any:
    """Recursively freeze mappings and sequences for immutable contracts."""

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): freeze(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return frozenset(freeze(item) for item in value)
    return value


def strict_json_loads(value: bytes | str) -> Any:
    """Decode JSON while rejecting duplicate keys and non-finite constants."""

    def object_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, item in pairs:
            if key in result:
                raise ContractError(f"duplicate JSON key: {key}")
            result[key] = item
        return result

    def reject_constant(raw: str) -> Any:
        raise ContractError(f"non-finite JSON constant is forbidden: {raw}")

    try:
        return json.loads(value, object_pairs_hook=object_pairs, parse_constant=reject_constant)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ContractError("invalid JSON document") from exc
