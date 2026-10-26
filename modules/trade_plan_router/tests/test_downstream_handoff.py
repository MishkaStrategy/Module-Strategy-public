from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json

import pytest

from modular_strategy_trade_plan_router import (
    DecisionRouter,
    DownstreamContractError,
    DownstreamPlanEnvelope,
    TradeAction,
    TradePlanSerializer,
)
from modular_strategy_trade_plan_router.models import ExecutionPrecondition

from conftest import NOW, make_package


def _plan():
    result = DecisionRouter().route(make_package())
    assert result.plan is not None
    return result.plan


def test_envelope_preserves_canonical_router_contract() -> None:
    plan = _plan()
    serializer = TradePlanSerializer()
    envelope = DownstreamPlanEnvelope.from_plan(plan, serializer=serializer)

    assert envelope.schema_version == plan.schema_version
    assert envelope.plan_id == plan.plan_id
    assert envelope.content_hash == plan.content_hash
    assert envelope.audit_hash == plan.audit_hash
    assert envelope.action is plan.action
    assert envelope.canonical_plan_json == serializer.dumps_plan(plan)
    assert len(envelope.payload_sha256) == 64
    assert envelope.preconditions == plan.preconditions

    loaded = serializer.loads_plan_mapping(envelope.canonical_plan_json)
    assert loaded["plan_id"] == plan.plan_id
    assert loaded["content_hash"] == plan.content_hash
    assert loaded["audit_hash"] == plan.audit_hash


def test_preconditions_remain_exact_strings_and_are_immutable() -> None:
    envelope = DownstreamPlanEnvelope.from_plan(_plan())
    expected = {item.kind: item.expected for item in envelope.preconditions}
    actual = envelope.precondition_mapping()

    assert dict(actual) == expected
    assert all(isinstance(value, str) for value in actual.values())
    with pytest.raises(TypeError):
        actual["portfolio_version"] = "mutated"  # type: ignore[index]


def test_envelope_is_idempotent_for_identical_plan() -> None:
    plan = _plan()
    first = DownstreamPlanEnvelope.from_plan(plan)
    second = DownstreamPlanEnvelope.from_plan(plan)

    assert first == second
    assert first.payload_sha256 == second.payload_sha256
    assert first.to_mapping() == second.to_mapping()


def test_payload_hash_detects_canonical_json_tampering() -> None:
    envelope = DownstreamPlanEnvelope.from_plan(_plan())
    payload = json.loads(envelope.canonical_plan_json)
    payload["entry_intent"]["quantity"] = "999"
    tampered = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )

    with pytest.raises(DownstreamContractError, match="payload SHA-256"):
        replace(envelope, canonical_plan_json=tampered)


def test_envelope_rejects_identifier_or_precondition_mismatch() -> None:
    envelope = DownstreamPlanEnvelope.from_plan(_plan())

    with pytest.raises(DownstreamContractError, match="plan_id"):
        replace(envelope, plan_id="different-plan")

    duplicate = (*envelope.preconditions, envelope.preconditions[0])
    with pytest.raises(DownstreamContractError, match="must be unique"):
        replace(envelope, preconditions=duplicate)

    changed = tuple(
        ExecutionPrecondition(item.kind, "different") if item.kind == "portfolio_version" else item
        for item in envelope.preconditions
    )
    with pytest.raises(DownstreamContractError, match="do not match"):
        replace(envelope, preconditions=changed)


def test_supported_action_gate_is_fail_closed() -> None:
    plan = _plan()
    envelope = DownstreamPlanEnvelope.from_plan(
        plan,
        supported_actions={TradeAction.OPEN_POSITION},
    )
    envelope.require_supported_actions({TradeAction.OPEN_POSITION})

    with pytest.raises(DownstreamContractError, match="does not support"):
        envelope.require_supported_actions({TradeAction.CLOSE_POSITION})
    with pytest.raises(DownstreamContractError, match="must contain"):
        envelope.require_supported_actions(set())
    with pytest.raises(DownstreamContractError, match="must contain"):
        envelope.require_supported_actions({"open_position"})  # type: ignore[arg-type]


def test_validity_window_is_rechecked_at_downstream_boundary() -> None:
    envelope = DownstreamPlanEnvelope.from_plan(_plan())

    envelope.validate_at(envelope.valid_from)
    envelope.validate_at(envelope.expires_at - timedelta(microseconds=1))

    with pytest.raises(DownstreamContractError, match="not yet valid"):
        envelope.validate_at(envelope.valid_from - timedelta(microseconds=1))
    with pytest.raises(DownstreamContractError, match="expired"):
        envelope.validate_at(envelope.expires_at)
    with pytest.raises(DownstreamContractError, match="timezone-aware"):
        envelope.validate_at(NOW.replace(tzinfo=None))


def test_factory_rejects_wrong_plan_or_serializer_types() -> None:
    with pytest.raises(DownstreamContractError, match="ExecutableTradePlan"):
        DownstreamPlanEnvelope.from_plan(object())  # type: ignore[arg-type]

    with pytest.raises(DownstreamContractError, match="TradePlanSerializer"):
        DownstreamPlanEnvelope.from_plan(
            _plan(),
            serializer=object(),  # type: ignore[arg-type]
        )


def test_mapping_exposes_payload_digest_and_typed_action() -> None:
    envelope = DownstreamPlanEnvelope.from_plan(_plan())
    mapping = envelope.to_mapping()

    assert mapping["action"] == TradeAction.OPEN_POSITION.value
    assert mapping["payload_sha256"] == envelope.payload_sha256
    assert mapping["canonical_plan_json"] == envelope.canonical_plan_json
    assert tuple((row["kind"], row["expected"]) for row in mapping["preconditions"]) == tuple(
        (item.kind, item.expected) for item in envelope.preconditions
    )
