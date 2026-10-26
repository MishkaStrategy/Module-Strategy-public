from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import fields, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import Enum
from types import MappingProxyType
from typing import Any


JSONScalar = None | bool | int | float | str
JSONValue = JSONScalar | list["JSONValue"] | dict[str, "JSONValue"]


MAX_CANONICAL_DECIMAL_TEXT_LENGTH = 1_000_000


def decimal_text(value: Decimal) -> str:
    """Return a context-independent fixed-point representation of ``value``.

    ``Decimal.normalize()`` applies the active decimal context and can round the
    coefficient. Canonical hashes must not depend on process-global precision or
    rounding, so formatting is derived directly from the immutable Decimal tuple.
    """
    if not value.is_finite():
        raise ValueError("non-finite Decimal is not canonicalizable")
    if value == 0:
        return "0"

    sign, digits_tuple, exponent = value.as_tuple()
    if not isinstance(exponent, int):
        raise ValueError("non-finite Decimal is not canonicalizable")
    digits = "".join(str(digit) for digit in digits_tuple) or "0"
    prefix_length = 1 if sign else 0

    if exponent >= 0:
        projected_length = prefix_length + len(digits) + exponent
        if projected_length > MAX_CANONICAL_DECIMAL_TEXT_LENGTH:
            raise ValueError(
                f"canonical Decimal exceeds maximum text length {MAX_CANONICAL_DECIMAL_TEXT_LENGTH}"
            )
        text = digits + ("0" * exponent)
    else:
        point = len(digits) + exponent
        if point > 0:
            projected_length = prefix_length + len(digits) + 1
            if projected_length > MAX_CANONICAL_DECIMAL_TEXT_LENGTH:
                raise ValueError(
                    "canonical Decimal exceeds maximum text length "
                    f"{MAX_CANONICAL_DECIMAL_TEXT_LENGTH}"
                )
            text = f"{digits[:point]}.{digits[point:]}"
        else:
            leading_zeros = -point
            projected_length = prefix_length + 2 + leading_zeros + len(digits)
            if projected_length > MAX_CANONICAL_DECIMAL_TEXT_LENGTH:
                raise ValueError(
                    "canonical Decimal exceeds maximum text length "
                    f"{MAX_CANONICAL_DECIMAL_TEXT_LENGTH}"
                )
            text = f"0.{('0' * leading_zeros)}{digits}"

    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return f"-{text}" if sign else text


def utc_text(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


MAX_CANONICAL_DEPTH = 64
MAX_CANONICAL_NODES = 200_000
MAX_CANONICAL_STRING_LENGTH = 1_000_000
MIN_CANONICAL_INTEGER = -9_223_372_036_854_775_808
MAX_CANONICAL_INTEGER = 9_223_372_036_854_775_807


def has_control_characters(value: str) -> bool:
    """Return whether text contains non-printable control/separator data.

    ``str.isprintable`` is implemented in C and preserves the established
    canonical boundary: ordinary spaces and visible Unicode are accepted, while
    control, format, private-use and line/paragraph separator characters are
    rejected before hashing or logging.
    """

    return bool(value) and not value.isprintable()


def _canonical_string(value: str, label: str = "canonical string") -> str:
    if len(value) > MAX_CANONICAL_STRING_LENGTH:
        raise ValueError(f"{label} exceeds maximum length {MAX_CANONICAL_STRING_LENGTH}")
    # CPython implements these predicates in C. The ASCII fast path avoids an
    # O(n) Unicode normalization pass for the overwhelmingly common identifiers
    # while preserving the exact same fail-closed text policy.
    if has_control_characters(value):
        raise ValueError(f"{label} cannot contain control characters")
    if not value.isascii() and not unicodedata.is_normalized("NFC", value):
        raise ValueError(f"{label} must use NFC normalization")
    return value


def _canonical_integer(value: int, label: str = "canonical integer") -> int:
    if not MIN_CANONICAL_INTEGER <= value <= MAX_CANONICAL_INTEGER:
        raise ValueError(
            f"{label} must be in signed 64-bit range "
            f"[{MIN_CANONICAL_INTEGER}, {MAX_CANONICAL_INTEGER}]"
        )
    return value


def _canonical_mapping_key(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("canonical mapping keys must be strings")
    return _canonical_string(value, "canonical mapping key")


def _canonical_mapping_items(
    value: Mapping[Any, Any],
) -> tuple[tuple[str, Any], ...]:
    result: list[tuple[str, Any]] = []
    seen: set[str] = set()
    for raw_key, member in value.items():
        key = _canonical_mapping_key(raw_key)
        if key in seen:
            raise ValueError(f"duplicate mapping key: {key!r}")
        seen.add(key)
        result.append((key, member))
    return tuple(result)


def canonical_value(value: Any) -> JSONValue:
    active: set[int] = set()
    nodes = 0

    def visit(item: Any, depth: int) -> JSONValue:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_CANONICAL_NODES:
            raise ValueError(f"canonical value exceeds maximum node count {MAX_CANONICAL_NODES}")
        # StrEnum subclasses str, so Enum must be normalized before scalar strings.
        if isinstance(item, Enum):
            return visit(item.value, depth)
        if isinstance(item, str):
            return _canonical_string(item)
        if item is None or isinstance(item, bool):
            return item
        if isinstance(item, int):
            return _canonical_integer(item)
        if isinstance(item, float):
            if item != item or item in {float("inf"), float("-inf")}:
                raise ValueError("non-finite float is not canonicalizable")
            return item
        if isinstance(item, Decimal):
            return decimal_text(item)
        if isinstance(item, datetime):
            return utc_text(item)

        is_data = is_dataclass(item) and not isinstance(item, type)
        is_mapping = isinstance(item, Mapping)
        is_set = isinstance(item, (set, frozenset))
        is_sequence = isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray))
        if not (is_data or is_mapping or is_set or is_sequence):
            raise TypeError(f"unsupported canonical value: {type(item).__name__}")
        if depth > MAX_CANONICAL_DEPTH:
            raise ValueError(f"canonical nesting exceeds maximum depth {MAX_CANONICAL_DEPTH}")
        identity = id(item)
        if identity in active:
            raise ValueError("cyclic canonical value is not supported")
        active.add(identity)
        try:
            if is_data:
                return {
                    field.name: visit(getattr(item, field.name), depth + 1)
                    for field in fields(item)
                    if not field.name.startswith("_")
                }
            if is_mapping:
                mapping_rows = _canonical_mapping_items(item)
                return {
                    key: visit(member, depth + 1)
                    for key, member in sorted(mapping_rows, key=lambda row: row[0])
                }
            if is_set:
                set_rows = [visit(member, depth + 1) for member in item]
                return sorted(
                    set_rows,
                    key=lambda row: json.dumps(
                        row,
                        ensure_ascii=False,
                        allow_nan=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ),
                )
            return [visit(member, depth + 1) for member in item]
        finally:
            active.remove(identity)

    return visit(value, 0)


def canonical_json(value: Any) -> str:
    return json.dumps(
        canonical_value(value),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def stable_id(prefix: str, value: Any, *, length: int = 32) -> str:
    if (
        not isinstance(prefix, str)
        or not prefix
        or not prefix.replace("-", "").replace("_", "").isalnum()
    ):
        raise ValueError("stable ID prefix must be non-blank and identifier-like")
    if not 8 <= length <= 64:
        raise ValueError("stable ID hash length must be in [8, 64]")
    return f"{prefix}-{sha256_hex(value)[:length]}"


def deep_freeze(value: Any) -> Any:
    active: set[int] = set()
    nodes = 0

    def freeze(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_CANONICAL_NODES:
            raise ValueError(f"mapping content exceeds maximum node count {MAX_CANONICAL_NODES}")
        is_mapping = isinstance(item, Mapping)
        is_sequence = isinstance(item, (list, tuple))
        is_set = isinstance(item, set)
        if not (is_mapping or is_sequence or is_set):
            if isinstance(item, Enum):
                freeze(item.value, depth)
                return item
            if isinstance(item, str):
                return _canonical_string(item, "mapping string")
            if item is None or isinstance(item, bool):
                return item
            if isinstance(item, int):
                return _canonical_integer(item, "mapping integer")
            if isinstance(item, float):
                if item != item or item in {float("inf"), float("-inf")}:
                    raise ValueError("non-finite float is not supported in mappings")
                return item
            if isinstance(item, Decimal):
                decimal_text(item)
                return item
            if isinstance(item, datetime):
                utc_text(item)
                return item
            raise TypeError(f"unsupported mapping value: {type(item).__name__}")
        if depth > MAX_CANONICAL_DEPTH:
            raise ValueError(f"canonical nesting exceeds maximum depth {MAX_CANONICAL_DEPTH}")
        identity = id(item)
        if identity in active:
            raise ValueError("cyclic canonical value is not supported")
        active.add(identity)
        try:
            if is_mapping:
                return MappingProxyType(
                    {
                        key: freeze(member, depth + 1)
                        for key, member in _canonical_mapping_items(item)
                    }
                )
            if isinstance(item, list):
                return tuple(freeze(member, depth + 1) for member in item)
            if isinstance(item, tuple):
                return tuple(freeze(member, depth + 1) for member in item)
            return frozenset(freeze(member, depth + 1) for member in item)
        finally:
            active.remove(identity)

    return freeze(value, 0)


def deep_thaw(value: Any) -> Any:
    active: set[int] = set()
    nodes = 0

    def thaw(item: Any, depth: int) -> Any:
        nonlocal nodes
        nodes += 1
        if nodes > MAX_CANONICAL_NODES:
            raise ValueError(f"mapping content exceeds maximum node count {MAX_CANONICAL_NODES}")
        is_mapping = isinstance(item, Mapping)
        is_sequence = isinstance(item, (tuple, list))
        is_set = isinstance(item, (set, frozenset))
        if not (is_mapping or is_sequence or is_set):
            if isinstance(item, Enum):
                thaw(item.value, depth)
                return item
            if isinstance(item, str):
                return _canonical_string(item, "mapping string")
            if item is None or isinstance(item, bool):
                return item
            if isinstance(item, int):
                return _canonical_integer(item, "mapping integer")
            if isinstance(item, float):
                if item != item or item in {float("inf"), float("-inf")}:
                    raise ValueError("non-finite float is not supported in mappings")
                return item
            if isinstance(item, Decimal):
                decimal_text(item)
                return item
            if isinstance(item, datetime):
                utc_text(item)
                return item
            raise TypeError(f"unsupported mapping value: {type(item).__name__}")
        if depth > MAX_CANONICAL_DEPTH:
            raise ValueError(f"canonical nesting exceeds maximum depth {MAX_CANONICAL_DEPTH}")
        identity = id(item)
        if identity in active:
            raise ValueError("cyclic canonical value is not supported")
        active.add(identity)
        try:
            if is_mapping:
                return {
                    key: thaw(member, depth + 1) for key, member in _canonical_mapping_items(item)
                }
            if is_sequence:
                return [thaw(member, depth + 1) for member in item]
            rows = [thaw(member, depth + 1) for member in item]
            return sorted(rows, key=canonical_json)
        finally:
            active.remove(identity)

    return thaw(value, 0)
