from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence, Set
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


class CanonicalizationError(ValueError):
    """Raised when a value cannot be represented deterministically."""


def _datetime_value(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise CanonicalizationError("datetime values must be timezone-aware")
    normalized = value.astimezone(UTC)
    return normalized.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _decimal_value(value: Decimal) -> str:
    if not value.is_finite():
        raise CanonicalizationError("Decimal values must be finite")
    if value == 0:
        return "0"
    normalized = value.normalize()
    rendered = format(normalized, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def canonical_value(value: Any) -> Any:
    """Return a JSON-safe, type-preserving canonical representation."""

    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise CanonicalizationError("float values must be finite")
        return {"$float_hex": value.hex()}
    if isinstance(value, Decimal):
        return {"$decimal": _decimal_value(value)}
    if isinstance(value, datetime):
        return {"$datetime_utc": _datetime_value(value)}
    if isinstance(value, timedelta):
        microseconds = value.days * 86_400_000_000 + value.seconds * 1_000_000 + value.microseconds
        return {"$timedelta_us": microseconds}
    if isinstance(value, Path):
        return {"$path": value.as_posix()}
    if isinstance(value, Enum):
        return canonical_value(value.value)
    if is_dataclass(value) and not isinstance(value, type):
        rendered: dict[str, Any] = {}
        for field in fields(value):
            item = getattr(value, field.name)
            if field.metadata.get("canonical_omit_none") is True and item is None:
                continue
            rendered[field.name] = canonical_value(item)
        return rendered
    if isinstance(value, Mapping):
        keys = tuple(value.keys())
        if any(not isinstance(key, str) or not key for key in keys):
            raise CanonicalizationError("mapping keys must be non-empty strings")
        return {key: canonical_value(value[key]) for key in sorted(keys)}
    if isinstance(value, Set) and not isinstance(value, str | bytes | bytearray):
        rendered_set = [canonical_value(item) for item in value]
        return sorted(rendered_set, key=canonical_json_bytes)
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        return [canonical_value(item) for item in value]
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


def restore_canonical_value(value: Any) -> Any:
    """Restore values emitted by :func:`canonical_value`.

    The decoder is intentionally limited to the tagged scalar types produced by this
    package and never imports or instantiates arbitrary classes.
    """

    if isinstance(value, list):
        return tuple(restore_canonical_value(item) for item in value)
    if not isinstance(value, dict):
        return value
    if set(value) == {"$float_hex"}:
        rendered = value["$float_hex"]
        if not isinstance(rendered, str):
            raise CanonicalizationError("invalid canonical float")
        restored_float = float.fromhex(rendered)
        if not math.isfinite(restored_float):
            raise CanonicalizationError("canonical float must be finite")
        return restored_float
    if set(value) == {"$decimal"}:
        rendered = value["$decimal"]
        if not isinstance(rendered, str):
            raise CanonicalizationError("invalid canonical decimal")
        restored_decimal = Decimal(rendered)
        if not restored_decimal.is_finite():
            raise CanonicalizationError("canonical decimal must be finite")
        return restored_decimal
    if set(value) == {"$datetime_utc"}:
        rendered = value["$datetime_utc"]
        if not isinstance(rendered, str) or not rendered.endswith("Z"):
            raise CanonicalizationError("invalid canonical datetime")
        try:
            return datetime.fromisoformat(rendered[:-1] + "+00:00").astimezone(UTC)
        except ValueError as exc:
            raise CanonicalizationError("invalid canonical datetime") from exc
    if set(value) == {"$timedelta_us"}:
        microseconds = value["$timedelta_us"]
        if not isinstance(microseconds, int) or isinstance(microseconds, bool):
            raise CanonicalizationError("invalid canonical timedelta")
        return timedelta(microseconds=microseconds)
    if set(value) == {"$path"}:
        rendered = value["$path"]
        if not isinstance(rendered, str):
            raise CanonicalizationError("invalid canonical path")
        return Path(rendered)
    return {key: restore_canonical_value(item) for key, item in value.items()}


def canonical_json_loads(payload: str | bytes | bytearray) -> Any:
    try:
        value = json.loads(payload)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CanonicalizationError("payload is not valid canonical JSON") from exc
    return restore_canonical_value(value)
