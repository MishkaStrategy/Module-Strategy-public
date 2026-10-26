from __future__ import annotations

from dataclasses import fields, is_dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
import json
import math
import re
from types import MappingProxyType
from typing import Any, Mapping

from .validation import utc_datetime


_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "access_key",
        "access_token",
        "authorization",
        "client_secret",
        "password",
        "passphrase",
        "private_key",
        "secret",
        "token",
    }
)
_SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_access_token",
    "_client_secret",
    "_password",
    "_passphrase",
    "_private_key",
    "_secret",
    "_token",
)
_SENSITIVE_TEXT_KEY_PATTERN = (
    r"api[_-]?key|access[_-]?(?:key|token)|authorization|"
    r"client[_-]?secret|password|passphrase|private[_-]?key|secret|token"
)
_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)\b({_SENSITIVE_TEXT_KEY_PATTERN})"
    r"(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;)}\]]+)"
)
_QUOTED_SECRET_ASSIGNMENT_RE = re.compile(
    rf"(?i)(?P<key_quote>['\"])(?P<key>{_SENSITIVE_TEXT_KEY_PATTERN})"
    r"(?P=key_quote)(?P<separator>\s*:\s*)"
    r"(?P<value>\"(?:\\.|[^\"])*\"|'(?:\\.|[^'])*'|[^\s,;)}\]]+)"
)
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")


def reject_sensitive_keys(value: Any, context: str = "value") -> None:
    """Reject secret-bearing mapping keys without exposing their values."""

    _reject_sensitive_keys(value, context, set(), depth=0)


def _reject_sensitive_keys(value: Any, context: str, active: set[int], *, depth: int) -> None:
    if depth > 256:
        raise ValueError(f"nesting depth exceeds limit in {context}")
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ValueError(f"cyclic mapping is not supported in {context}")
        active.add(marker)
        try:
            for key, item in value.items():
                string_key = _require_string_key(key, context)
                if _is_sensitive_key(string_key):
                    raise ValueError(
                        f"sensitive parameter key is not allowed in {context}: {string_key!r}"
                    )
                _reject_sensitive_keys(item, f"{context}.{string_key}", active, depth=depth + 1)
        finally:
            active.remove(marker)
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        marker = id(value)
        if marker in active:
            raise ValueError(f"cyclic sequence is not supported in {context}")
        active.add(marker)
        try:
            for index, item in enumerate(value):
                _reject_sensitive_keys(item, f"{context}[{index}]", active, depth=depth + 1)
        finally:
            active.remove(marker)


def _is_sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")
    return normalized in _SENSITIVE_KEY_NAMES or normalized.endswith(_SENSITIVE_KEY_SUFFIXES)


def redact_sensitive_text(value: str) -> str:
    """Redact common credential assignments from diagnostic text."""

    if not isinstance(value, str):
        raise TypeError("diagnostic text must be str")

    def redact_quoted(match: re.Match[str]) -> str:
        key_quote = match.group("key_quote")
        raw_value = match.group("value")
        value_quote = raw_value[0] if raw_value and raw_value[0] in {"'", '"'} else key_quote
        return (
            f"{key_quote}{match.group('key')}{key_quote}"
            f"{match.group('separator')}{value_quote}[REDACTED]{value_quote}"
        )

    redacted = _QUOTED_SECRET_ASSIGNMENT_RE.sub(redact_quoted, value)
    redacted = _BEARER_RE.sub("Bearer [REDACTED]", redacted)
    return _SECRET_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted
    )


def _require_string_key(value: object, context: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{context} keys must be str")
    _validate_json_string(value)
    return value


def strict_json_loads(value: str | bytes | bytearray) -> Any:
    """Decode canonical JSON while rejecting ambiguous or unsafe values."""

    try:
        decoded = json.loads(
            value,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except RecursionError as exc:
        raise ValueError("JSON nesting depth exceeds limit") from exc
    _validate_decoded_json(decoded, depth=0)
    return decoded


def _reject_json_constant(value: str) -> Any:
    raise ValueError(f"non-finite JSON constant is not allowed: {value}")


def _validate_decoded_json(value: Any, *, depth: int) -> None:
    if depth > 256:
        raise ValueError("JSON nesting depth exceeds limit")
    if isinstance(value, dict):
        for key, item in value.items():
            _validate_json_string(key)
            _validate_decoded_json(item, depth=depth + 1)
        return
    if isinstance(value, list):
        for item in value:
            _validate_decoded_json(item, depth=depth + 1)
        return
    if isinstance(value, str):
        _validate_json_string(value)
        return
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("decoded JSON numbers must be finite")
    if value is None or isinstance(value, (bool, int, float)):
        return
    raise TypeError(f"unsupported decoded JSON value type: {type(value).__name__}")


def _validate_json_string(value: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("JSON strings must contain valid Unicode scalar values") from exc


def _strict_json_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def deep_freeze(value: Any) -> Any:
    return _deep_freeze(value, set(), depth=0)


def _deep_freeze(value: Any, active: set[int], *, depth: int) -> Any:
    if depth > 256:
        raise ValueError("nesting depth exceeds limit while freezing value")
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic mapping is not supported")
        active.add(marker)
        try:
            return MappingProxyType(
                {
                    _require_string_key(key, "mapping"): _deep_freeze(item, active, depth=depth + 1)
                    for key, item in value.items()
                }
            )
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic sequence is not supported")
        active.add(marker)
        try:
            return tuple(_deep_freeze(item, active, depth=depth + 1) for item in value)
        finally:
            active.remove(marker)
    if isinstance(value, (set, frozenset)):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic set is not supported")
        active.add(marker)
        try:
            return frozenset(_deep_freeze(item, active, depth=depth + 1) for item in value)
        finally:
            active.remove(marker)
    return value


def to_jsonable(value: Any) -> Any:
    return _to_jsonable(value, set(), depth=0)


def _to_jsonable(value: Any, active: set[int], *, depth: int) -> Any:
    if depth > 256:
        raise ValueError("JSON nesting depth exceeds limit")
    if is_dataclass(value):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic dataclass is not supported")
        active.add(marker)
        try:
            return {
                item.name: _to_jsonable(getattr(value, item.name), active, depth=depth + 1)
                for item in fields(value)
            }
        finally:
            active.remove(marker)
    if isinstance(value, Mapping):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic mapping is not supported")
        active.add(marker)
        try:
            return {
                _require_string_key(key, "JSON mapping"): _to_jsonable(
                    item, active, depth=depth + 1
                )
                for key, item in value.items()
            }
        finally:
            active.remove(marker)
    if isinstance(value, (list, tuple)):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic sequence is not supported")
        active.add(marker)
        try:
            return [_to_jsonable(item, active, depth=depth + 1) for item in value]
        finally:
            active.remove(marker)
    if isinstance(value, (set, frozenset)):
        marker = id(value)
        if marker in active:
            raise ValueError("cyclic set is not supported")
        active.add(marker)
        try:
            items = [_to_jsonable(item, active, depth=depth + 1) for item in value]
        finally:
            active.remove(marker)
        return sorted(
            items,
            key=lambda item: json.dumps(
                item,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=True,
                allow_nan=False,
            ),
        )
    if isinstance(value, datetime):
        return utc_datetime(value, "JSON datetime").isoformat()
    if isinstance(value, Enum):
        return _to_jsonable(value.value, active, depth=depth + 1)
    if isinstance(value, (float, Decimal)):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("JSON values must be finite")
        return 0.0 if number == 0.0 else number
    if isinstance(value, str):
        _validate_json_string(value)
        return value
    if value is None or isinstance(value, (int, bool)):
        return value
    raise TypeError(f"unsupported JSON value type: {type(value).__name__}")


def freeze_jsonable(value: Any) -> Any:
    """Return detached immutable canonical JSON evidence."""

    return deep_freeze(to_jsonable(value))
