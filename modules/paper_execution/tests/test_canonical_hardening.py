from __future__ import annotations

from decimal import Decimal

import pytest

from paper_execution import _utils
from paper_execution._utils import canonical_json


def test_mapping_keys_must_be_strings_to_prevent_hash_aliases() -> None:
    with pytest.raises(TypeError, match="keys must be strings"):
        canonical_json({1: "numeric-key"})
    assert canonical_json({"1": "string-key"}) == '{"1":"string-key"}'


def test_cycles_are_rejected_without_recursion_error() -> None:
    cyclic: list[object] = []
    cyclic.append(cyclic)

    with pytest.raises(ValueError, match="contains a cycle"):
        canonical_json(cyclic)


def test_depth_limit_is_fail_closed(monkeypatch) -> None:
    monkeypatch.setattr(_utils, "MAX_CANONICAL_DEPTH", 3)
    nested: object = "leaf"
    for _ in range(5):
        nested = [nested]

    with pytest.raises(ValueError, match="nesting depth"):
        canonical_json(nested)


def test_container_limit_is_checked_before_traversal(monkeypatch) -> None:
    monkeypatch.setattr(_utils, "MAX_CANONICAL_CONTAINER_ITEMS", 1)

    with pytest.raises(ValueError, match="sequence exceeds item limit"):
        canonical_json(["a", object()])
    with pytest.raises(ValueError, match="mapping exceeds item limit"):
        canonical_json({"a": 1, "b": object()})


def test_non_finite_decimals_and_floats_are_rejected_cleanly() -> None:
    for value in (Decimal("NaN"), Decimal("Infinity")):
        with pytest.raises(ValueError, match="non-finite decimal"):
            canonical_json(value)
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError, match="non-finite float"):
            canonical_json(value)


def test_scalar_limits_prevent_pathological_rendering(monkeypatch) -> None:
    monkeypatch.setattr(_utils, "MAX_CANONICAL_SCALAR_CHARS", 4)
    monkeypatch.setattr(_utils, "MAX_CANONICAL_INTEGER_BITS", 4)

    with pytest.raises(ValueError, match="string is too large"):
        canonical_json("12345")
    with pytest.raises(ValueError, match="mapping key is too large"):
        canonical_json({"12345": 1})
    with pytest.raises(ValueError, match="integer representation is too large"):
        canonical_json(1 << 5)
    with pytest.raises(ValueError, match="decimal representation is too large"):
        canonical_json(Decimal("12345"))


def test_shared_child_without_cycle_remains_supported() -> None:
    shared = {"value": Decimal("1.25")}
    assert canonical_json({"a": shared, "b": shared}) == (
        '{"a":{"value":"1.25"},"b":{"value":"1.25"}}'
    )
