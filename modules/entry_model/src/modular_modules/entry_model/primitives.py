from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType

from modular_core import Side

from .enums import EntryKind, SetupPayloadError

JSONScalar = None | str | bool | int | float
JSONValue = JSONScalar | tuple["JSONValue", ...] | Mapping[str, "JSONValue"]


def parse_side(value: object) -> Side:
    if isinstance(value, Side):
        return value
    try:
        return Side(str(value).strip().lower())
    except (TypeError, ValueError) as exc:
        raise SetupPayloadError("side must be 'buy' or 'sell'") from exc


def parse_entry_kind(value: object) -> EntryKind:
    if isinstance(value, EntryKind):
        return value
    try:
        return EntryKind(str(value).strip().lower())
    except (TypeError, ValueError) as exc:
        raise SetupPayloadError(f"unsupported entry kind: {value!r}") from exc


def parse_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip().replace("Z", "+00:00")
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise SetupPayloadError(f"{name} must be an ISO-8601 datetime") from exc
    else:
        raise SetupPayloadError(f"{name} must be a timezone-aware datetime")
    require_aware(result, name)
    return result.astimezone(UTC)


def optional_datetime(value: object, name: str) -> datetime | None:
    return None if value is None else parse_datetime(value, name)


def parse_decimal(value: object, name: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool) or value is None:
        comparator = "positive" if positive else "finite"
        raise SetupPayloadError(f"{name} must be a {comparator} number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise SetupPayloadError(f"{name} must be numeric") from exc
    if not result.is_finite() or (positive and result <= 0):
        comparator = "positive finite" if positive else "finite"
        raise SetupPayloadError(f"{name} must be a {comparator} number")
    return result


def optional_decimal(value: object, name: str) -> Decimal | None:
    return None if value is None else parse_decimal(value, name)


def required_text(value: object, name: str) -> str:
    if isinstance(value, bool):
        raise SetupPayloadError(f"{name} is required")
    text = str(value or "").strip()
    if not text:
        raise SetupPayloadError(f"{name} is required")
    return text


def require_aware(value: datetime, name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise SetupPayloadError(f"{name} must be timezone-aware")


def require_positive_decimal(value: Decimal, name: str) -> None:
    if not isinstance(value, Decimal) or not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be a positive finite Decimal")


def decimal_text(value: Decimal) -> str:
    if not value.is_finite():
        raise ValueError("Decimal must be finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def optional_decimal_text(value: Decimal | None) -> str | None:
    return None if value is None else decimal_text(value)


def json_safe_mapping[K, V](value: Mapping[K, V], name: str) -> Mapping[str, JSONValue]:
    result: dict[str, JSONValue] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            raise ValueError(f"{name} keys must be strings")
        result[key] = json_safe_value(item, f"{name}.{key}")
    return MappingProxyType(result)


def json_safe_value(value: object, name: str) -> JSONValue:
    if value is None or isinstance(value, str | bool | int):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} float must be finite")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} Decimal must be finite")
        return decimal_text(value)
    if isinstance(value, datetime):
        require_aware(value, name)
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Enum):
        return json_safe_value(value.value, name)
    if isinstance(value, Mapping):
        return json_safe_mapping(value, name)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return tuple(json_safe_value(item, f"{name}[]") for item in value)
    raise ValueError(f"{name} contains unsupported value {type(value).__name__}")


def primitive_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): primitive_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [primitive_json(item) for item in value]
    return value


def canonical_json(value: object) -> str:
    safe = json_safe_value(value, "payload")
    return json.dumps(
        primitive_json(safe),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
