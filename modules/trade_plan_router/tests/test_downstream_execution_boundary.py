from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    ExecutableTradePlan,
    RouterInputPackage,
    RouterSerializationError,
    TradePlanSerializer,
)


def _accepted_plan(package: RouterInputPackage) -> ExecutableTradePlan:
    result = DecisionRouter().route(package)
    assert result.plan is not None
    return result.plan


def _mutable_payload(plan: ExecutableTradePlan) -> dict[str, Any]:
    value = json.loads(TradePlanSerializer().dumps_plan(plan))
    assert isinstance(value, dict)
    return value


def _preconditions(value: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = value["preconditions"]
    assert isinstance(raw, list)
    assert all(isinstance(item, dict) for item in raw)
    return raw


def test_canonical_plan_json_is_the_downstream_contract(
    package: RouterInputPackage,
) -> None:
    plan = _accepted_plan(package)
    serializer = TradePlanSerializer()

    payload = serializer.dumps_plan(plan)
    decoded = serializer.loads_plan_mapping(payload)
    decoded_bytes = serializer.loads_plan_mapping(payload.encode("utf-8"))

    assert decoded_bytes == decoded
    assert decoded["schema_version"] == "1"
    assert decoded["plan_id"] == plan.plan_id
    assert decoded["content_hash"] == plan.content_hash
    assert decoded["audit_hash"] == plan.audit_hash
    assert decoded["configuration_hash"] == plan.configuration_hash
    assert tuple(decoded["input_hashes"]) == plan.input_hashes


def test_downstream_boundary_preserves_executable_intent_semantics(
    package: RouterInputPackage,
) -> None:
    plan = _accepted_plan(package)
    serializer = TradePlanSerializer()
    payload = serializer.dumps_plan(plan)
    decoded = serializer.loads_plan_mapping(payload)
    json_mapping = json.loads(payload)
    plan_mapping = plan.to_mapping()

    assert decoded["action"] == plan.action.value
    assert decoded["side"] == plan.side.value
    assert json_mapping["entry_intent"] == plan_mapping["entry_intent"]
    assert json_mapping["protection_intent"] == plan_mapping["protection_intent"]
    assert json_mapping["sizing_intent"] == plan_mapping["sizing_intent"]
    assert tuple(decoded["sequencing_rules"]) == plan.sequencing_rules
    assert tuple(decoded["input_hashes"]) == plan.input_hashes


def test_tampered_occ_precondition_is_rejected_before_execution(
    package: RouterInputPackage,
) -> None:
    plan = _accepted_plan(package)
    value = _mutable_payload(plan)
    preconditions = _preconditions(value)
    portfolio_version = next(item for item in preconditions if item["kind"] == "portfolio_version")
    portfolio_version["expected"] = "tampered-portfolio-version"

    with pytest.raises(RouterSerializationError):
        TradePlanSerializer().loads_plan_mapping(json.dumps(value))


def test_duplicate_precondition_is_rejected_before_execution(
    package: RouterInputPackage,
) -> None:
    plan = _accepted_plan(package)
    value = _mutable_payload(plan)
    preconditions = _preconditions(value)
    duplicate = dict(preconditions[0])
    preconditions.append(duplicate)

    with pytest.raises(RouterSerializationError):
        TradePlanSerializer().loads_plan_mapping(json.dumps(value))


def test_tampered_quantity_is_rejected_instead_of_recalculated(
    package: RouterInputPackage,
) -> None:
    plan = _accepted_plan(package)
    value = _mutable_payload(plan)
    entry_intent = value["entry_intent"]
    assert isinstance(entry_intent, dict)
    entry_intent["quantity"] = "999"

    with pytest.raises(RouterSerializationError):
        TradePlanSerializer().loads_plan_mapping(json.dumps(value))
