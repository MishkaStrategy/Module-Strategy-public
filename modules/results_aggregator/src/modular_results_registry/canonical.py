from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from decimal import Decimal
from enum import Enum
from pathlib import PurePath
from types import MappingProxyType
from typing import Any

from .errors import ValidationError


class CanonicalizationError(ValidationError):
    """A value cannot be represented in deterministic JSON."""


def deep_freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        frozen: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("mapping keys must be strings")
            frozen[key] = deep_freeze(item)
        return MappingProxyType(frozen)
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return tuple(deep_freeze(item) for item in value)
    if isinstance(value, set | frozenset):
        return tuple(sorted((deep_freeze(item) for item in value), key=canonical_json_bytes))
    return value


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("non-finite floats are forbidden")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise CanonicalizationError("non-finite decimals are forbidden")
        decimal_value = value.normalize()
        return format(decimal_value, "f") if decimal_value != 0 else "0"
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise CanonicalizationError("naive datetime is forbidden")
        return (
            value.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        )
    if isinstance(value, Enum):
        return _normalize(value.value)
    if isinstance(value, PurePath):
        return value.as_posix()
    if is_dataclass(value) and not isinstance(value, type):
        return {item.name: _normalize(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        normalized_mapping: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise CanonicalizationError("mapping keys must be strings")
            normalized_mapping[key] = _normalize(item)
        return normalized_mapping
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, memoryview)):
        return [_normalize(item) for item in value]
    raise CanonicalizationError(f"unsupported type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _normalize(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_json_text(value: Any) -> str:
    """Return deterministic JSON as UTF-8 text.

    This compatibility facade deliberately delegates to the byte canonicalizer so
    there is only one canonical representation.
    """
    return canonical_json_bytes(value).decode("utf-8")


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def deterministic_id(prefix: str, value: Any, *, digest_chars: int = 32) -> str:
    if not prefix or not prefix.replace("-", "").replace("_", "").isalnum():
        raise CanonicalizationError("identifier prefix must be alphanumeric with '-' or '_'")
    if digest_chars < 16 or digest_chars > 64:
        raise CanonicalizationError("digest_chars must be between 16 and 64")
    return f"{prefix}-{canonical_sha256(value)[:digest_chars]}"
