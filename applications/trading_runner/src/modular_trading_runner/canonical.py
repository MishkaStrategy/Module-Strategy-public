from __future__ import annotations

import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from hashlib import sha256
from typing import Any


def to_jsonable(value: object) -> object:
    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float is not canonical JSON")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite Decimal is not canonical JSON")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("naive datetime is not canonical JSON")
        return {"$datetime": value.astimezone(UTC).isoformat()}
    if isinstance(value, Enum):
        return to_jsonable(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: to_jsonable(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, Mapping):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON mapping keys must be strings")
            normalized[key] = to_jsonable(item)
        return {key: normalized[key] for key in sorted(normalized)}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [to_jsonable(item) for item in value]
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def from_jsonable(value: object) -> object:
    if value is None or isinstance(value, (bool, str, int, float)):
        return value
    if isinstance(value, Mapping):
        if set(value) == {"$datetime"}:
            raw = value["$datetime"]
            if not isinstance(raw, str):
                raise ValueError("canonical datetime value must be a string")
            parsed = datetime.fromisoformat(raw)
            if parsed.tzinfo is None or parsed.utcoffset() is None:
                raise ValueError("canonical datetime must be timezone-aware")
            return parsed.astimezone(UTC)
        if set(value) == {"$decimal"}:
            raw = value["$decimal"]
            if not isinstance(raw, str):
                raise ValueError("canonical decimal value must be a string")
            try:
                parsed_decimal = Decimal(raw)
            except InvalidOperation as exc:
                raise ValueError("canonical decimal is invalid") from exc
            if not parsed_decimal.is_finite():
                raise ValueError("canonical decimal must be finite")
            return parsed_decimal
        restored: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical JSON mapping keys must be strings")
            restored[key] = from_jsonable(item)
        return restored
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(from_jsonable(item) for item in value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        to_jsonable(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def canonical_sha256(value: object) -> str:
    return sha256(canonical_json(value).encode("utf-8")).hexdigest()


def require_json_mapping(value: object, name: str) -> Mapping[str, Any]:
    normalized = to_jsonable(value)
    if not isinstance(normalized, Mapping):
        raise TypeError(f"{name} must normalize to a mapping")
    return normalized
