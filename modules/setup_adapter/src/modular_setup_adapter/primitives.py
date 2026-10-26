from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any


def decimal_value(value: object, name: str, *, positive: bool = True) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be numeric")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be numeric") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    if positive and result <= 0:
        raise ValueError(f"{name} must be positive")
    return result


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def aware_datetime(value: object, name: str) -> datetime:
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError(f"{name} is required")
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            result = datetime.fromisoformat(text)
        except ValueError as exc:
            raise ValueError(f"{name} must be ISO-8601") from exc
    else:
        raise ValueError(f"{name} must be timezone-aware ISO-8601")
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")
    return result.astimezone(UTC)


def required_text(value: object, name: str) -> str:
    if value is None:
        raise ValueError(f"{name} is required")
    result = str(value).strip()
    if not result:
        raise ValueError(f"{name} is required")
    return result


def primitive(value: object, name: str = "value") -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{name} contains non-finite float")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{name} contains non-finite Decimal")
        return decimal_text(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(f"{name} contains naive datetime")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{name} mapping keys must be strings")
            result[key] = primitive(item, f"{name}.{key}")
        return result
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [primitive(item, f"{name}[]") for item in value]
    raise ValueError(f"{name} contains unsupported type {type(value).__name__}")


def canonical_json(value: object) -> str:
    return json.dumps(
        primitive(value),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def source_feature_fingerprint(feature: Any) -> str:
    return sha256_hex(
        {
            "key": feature.key,
            "source_module": feature.source_module,
            "created_at": feature.created_at,
            "symbol": feature.symbol,
            "timeframe": feature.timeframe,
            "valid_until": feature.valid_until,
            "schema_version": feature.schema_version,
            "feature_id": feature.feature_id,
            "value": feature.value,
            "metadata": feature.metadata,
        }
    )
