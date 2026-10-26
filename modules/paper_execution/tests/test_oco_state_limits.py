from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from paper_execution._utils import canonical_json, sha256_text
from paper_execution.exceptions import OrderStateError
from paper_execution.oco import OcoGroup


def _rehash(state: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))
    return state


def test_restore_rejects_non_string_group_id_even_with_valid_hash() -> None:
    group = OcoGroup("group", Decimal("1"))
    group.register("stop", Decimal("1"))
    state = deepcopy(dict(group.snapshot_state()))
    state["group_id"] = None
    _rehash(state)

    with pytest.raises(ValueError, match="group_id"):
        OcoGroup.from_state(state)


def test_restore_rejects_blank_member_identifier_even_with_valid_hash() -> None:
    group = OcoGroup("group", Decimal("1"))
    group.register("stop", Decimal("1"))
    state = deepcopy(dict(group.snapshot_state()))
    state["members"] = {" ": "1"}
    state["filled"] = {" ": "0"}
    _rehash(state)

    with pytest.raises(ValueError, match="member identifier"):
        OcoGroup.from_state(state)


def test_restore_rejects_unhashable_canceled_identifier_cleanly() -> None:
    group = OcoGroup("group", Decimal("1"))
    group.register("stop", Decimal("1"))
    state = deepcopy(dict(group.snapshot_state()))
    state["canceled"] = [{}]

    with pytest.raises(ValueError, match="invalid canceled"):
        OcoGroup.from_state(state)


def test_runtime_member_limit_is_fail_closed(monkeypatch) -> None:
    import paper_execution.oco as oco_module

    monkeypatch.setattr(oco_module, "MAX_OCO_MEMBERS", 1)
    group = OcoGroup("group", Decimal("1"))
    group.register("stop", Decimal("1"))

    with pytest.raises(OrderStateError, match="member limit"):
        group.register("target", Decimal("1"))
    assert set(group.members) == {"stop"}
    assert set(group.filled) == {"stop"}


def test_restore_checks_limit_before_hashing_large_member_state(monkeypatch) -> None:
    import paper_execution.oco as oco_module

    monkeypatch.setattr(oco_module, "MAX_OCO_MEMBERS", 1)
    state = {
        "schema_version": "1",
        "group_id": "group",
        "position_quantity": "1",
        "members": {"stop": "1", "target": "1"},
        "filled": {"stop": "0", "target": "0"},
        "canceled": [],
        "state_hash": "not-evaluated",
    }

    with pytest.raises(ValueError, match="member limit"):
        OcoGroup.from_state(state)
