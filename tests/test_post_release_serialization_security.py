from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from types import MappingProxyType

import pytest

from modular_core.serialization import (
    deep_freeze,
    freeze_jsonable,
    redact_sensitive_text,
    reject_sensitive_keys,
    strict_json_loads,
    to_jsonable,
)


def test_reject_sensitive_keys_walks_nested_collections() -> None:
    reject_sensitive_keys(
        {
            "rows": [{"period": 14}],
            "labels": ("safe",),
            "symbols": frozenset({"BTCUSDT"}),
        }
    )

    with pytest.raises(ValueError, match="sensitive parameter key"):
        reject_sensitive_keys({"outer": [{"api-key": "secret"}]})


def test_reject_sensitive_keys_rejects_invalid_keys_and_cycles() -> None:
    with pytest.raises(TypeError, match="keys must be str"):
        reject_sensitive_keys({1: "ambiguous"})

    mapping: dict[str, object] = {}
    mapping["self"] = mapping
    with pytest.raises(ValueError, match="cyclic mapping"):
        reject_sensitive_keys(mapping)

    sequence: list[object] = []
    sequence.append(sequence)
    with pytest.raises(ValueError, match="cyclic sequence"):
        reject_sensitive_keys(sequence)


def test_reject_sensitive_keys_enforces_depth_limit() -> None:
    root: list[object] = []
    cursor = root
    for _ in range(258):
        child: list[object] = []
        cursor.append(child)
        cursor = child

    with pytest.raises(ValueError, match="nesting depth"):
        reject_sensitive_keys(root)


def test_redactor_handles_bearer_and_unquoted_assignments() -> None:
    text = "Authorization: Bearer abc.def token=plain-secret"
    redacted = redact_sensitive_text(text)

    assert "abc.def" not in redacted
    assert "plain-secret" not in redacted
    assert redacted.count("[REDACTED]") >= 2

    with pytest.raises(TypeError, match="diagnostic text must be str"):
        redact_sensitive_text(1)  # type: ignore[arg-type]


def test_strict_json_loads_accepts_canonical_json() -> None:
    assert strict_json_loads('{"outer":{"rows":[1,true,null]}}') == {
        "outer": {"rows": [1, True, None]}
    }


@pytest.mark.parametrize(
    "payload",
    [
        '{"duplicate":1,"duplicate":2}',
        '{"value":NaN}',
        '"\\ud800"',
    ],
)
def test_strict_json_loads_rejects_ambiguous_or_invalid_values(payload: str) -> None:
    with pytest.raises(ValueError):
        strict_json_loads(payload)


def test_deep_freeze_detaches_and_immutably_normalizes_collections() -> None:
    source = {"rows": [1, 2], "labels": {"b", "a"}}
    frozen = deep_freeze(source)

    assert isinstance(frozen, MappingProxyType)
    assert frozen["rows"] == (1, 2)
    assert frozen["labels"] == frozenset({"a", "b"})
    source["rows"].append(3)  # type: ignore[union-attr]
    assert frozen["rows"] == (1, 2)


def test_deep_freeze_and_to_jsonable_reject_cycles() -> None:
    mapping: dict[str, object] = {}
    mapping["self"] = mapping
    sequence: list[object] = []
    sequence.append(sequence)

    for operation, value in (
        (deep_freeze, mapping),
        (deep_freeze, sequence),
        (to_jsonable, mapping),
        (to_jsonable, sequence),
    ):
        with pytest.raises(ValueError, match="cyclic"):
            operation(value)


class Probe(Enum):
    VALUE = "value"


@dataclass
class Evidence:
    timestamp: datetime
    amount: Decimal
    state: Probe


def test_to_jsonable_normalizes_supported_boundary_types() -> None:
    value = Evidence(
        timestamp=datetime(
            2026,
            1,
            1,
            3,
            tzinfo=timezone(timedelta(hours=3)),
        ),
        amount=Decimal("-0"),
        state=Probe.VALUE,
    )

    assert to_jsonable(value) == {
        "timestamp": "2026-01-01T00:00:00+00:00",
        "amount": 0.0,
        "state": "value",
    }


def test_to_jsonable_rejects_cyclic_dataclass_and_invalid_unicode() -> None:
    @dataclass
    class Node:
        child: object | None = None

    node = Node()
    node.child = node
    with pytest.raises(ValueError, match="cyclic dataclass"):
        to_jsonable(node)

    with pytest.raises(ValueError, match="Unicode scalar"):
        to_jsonable("\ud800")


def test_freeze_jsonable_returns_detached_canonical_evidence() -> None:
    source = {"rows": [3, 1, 2], "labels": {"b", "a"}}
    evidence = freeze_jsonable(source)

    assert isinstance(evidence, MappingProxyType)
    assert evidence["rows"] == (3, 1, 2)
    assert evidence["labels"] == ("a", "b")
    source["rows"].append(4)  # type: ignore[union-attr]
    assert evidence["rows"] == (3, 1, 2)
