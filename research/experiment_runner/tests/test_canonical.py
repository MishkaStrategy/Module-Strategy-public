from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from modular_research_runner import (
    CanonicalizationError,
    canonical_json_bytes,
    canonical_sha256,
)


def test_mapping_order_does_not_change_hash() -> None:
    left = {"b": 2, "a": Decimal("1.500")}
    right = {"a": Decimal("1.5"), "b": 2}
    assert canonical_sha256(left) == canonical_sha256(right)


def test_types_are_preserved_in_canonical_form() -> None:
    assert canonical_sha256("1") != canonical_sha256(1)
    assert canonical_sha256(1.0) != canonical_sha256(1)
    assert canonical_sha256(Decimal("1")) != canonical_sha256("1")


def test_datetime_normalizes_to_utc() -> None:
    utc = datetime(2024, 1, 1, 12, tzinfo=UTC)
    assert b"2024-01-01T12:00:00.000000Z" in canonical_json_bytes(utc)


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="timezone-aware"):
        canonical_sha256(datetime(2024, 1, 1))


def test_non_finite_numbers_are_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="finite"):
        canonical_sha256(float("nan"))
    with pytest.raises(CanonicalizationError, match="finite"):
        canonical_sha256(Decimal("Infinity"))


def test_path_and_set_are_deterministic() -> None:
    first = {"path": Path("a/b"), "items": {"z", "a"}}
    second = {"items": {"a", "z"}, "path": Path("a/b")}
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_unsupported_value_is_rejected() -> None:
    with pytest.raises(CanonicalizationError, match="unsupported"):
        canonical_sha256(object())


def test_canonical_decoder_restores_supported_scalar_types() -> None:
    from datetime import timedelta
    from decimal import Decimal
    from pathlib import Path

    from modular_research_runner.canonical import canonical_json_bytes, canonical_json_loads

    value = {
        "decimal": Decimal("0.4500"),
        "float": 0.45,
        "path": Path("runs/output"),
        "duration": timedelta(seconds=3, microseconds=5),
    }
    restored = canonical_json_loads(canonical_json_bytes(value))
    assert restored == {
        "decimal": Decimal("0.45"),
        "float": 0.45,
        "path": Path("runs/output"),
        "duration": timedelta(seconds=3, microseconds=5),
    }


@pytest.mark.parametrize(
    "payload, message",
    [
        (b"not-json", "valid canonical"),
        (b'{"$float_hex": 1}', "canonical float"),
        (b'{"$float_hex": "inf"}', "finite"),
        (b'{"$decimal": 1}', "canonical decimal"),
        (b'{"$datetime_utc": "2026-01-01"}', "canonical datetime"),
        (b'{"$timedelta_us": true}', "canonical timedelta"),
        (b'{"$path": 2}', "canonical path"),
    ],
)
def test_canonical_decoder_rejects_malformed_tags(payload: bytes, message: str) -> None:
    from modular_research_runner.canonical import CanonicalizationError, canonical_json_loads

    with pytest.raises(CanonicalizationError, match=message):
        canonical_json_loads(payload)
