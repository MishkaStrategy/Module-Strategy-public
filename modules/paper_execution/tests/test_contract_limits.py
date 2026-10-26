from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest
from modular_core import OrderType, Side

from paper_execution import ExecutableTradePlan, ExecutionIntent, IntentRole
from paper_execution import contracts as contracts_module


def test_intent_metadata_requires_string_keys_and_acyclic_values() -> None:
    with pytest.raises(TypeError, match="metadata key"):
        ExecutionIntent(
            intent_id="metadata-key",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            metadata={1: "value"},
        )

    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    with pytest.raises(ValueError, match="contains a cycle"):
        ExecutionIntent(
            intent_id="metadata-cycle",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            metadata=cyclic,
        )


def test_intent_flags_and_target_index_are_strict_types() -> None:
    with pytest.raises(TypeError, match="post_only must be bool"):
        ExecutionIntent(
            intent_id="bool-flag",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            post_only=1,
        )
    with pytest.raises(TypeError, match="target_index must be int"):
        ExecutionIntent(
            intent_id="bool-index",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            target_index=True,
        )


def test_order_intent_cannot_smuggle_cancel_selector() -> None:
    with pytest.raises(ValueError, match="cannot contain cancel"):
        ExecutionIntent(
            intent_id="entry-with-cancel",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            cancel_client_order_id="existing-order",
        )


def test_intent_metadata_item_and_size_limits(monkeypatch) -> None:
    monkeypatch.setattr(contracts_module, "MAX_INTENT_METADATA_ITEMS", 1)
    with pytest.raises(ValueError, match="item limit"):
        ExecutionIntent(
            intent_id="metadata-count",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            metadata={"a": 1, "b": 2},
        )

    monkeypatch.setattr(contracts_module, "MAX_INTENT_METADATA_CHARS", 8)
    with pytest.raises(ValueError, match="canonical size"):
        ExecutionIntent(
            intent_id="metadata-size",
            role=IntentRole.ENTRY,
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=Decimal("1"),
            metadata={"value": "long-value"},
        )


def test_plan_rejects_non_intents_and_wrong_state_version_type(plan) -> None:
    with pytest.raises(TypeError, match="ExecutionIntent"):
        replace(plan, intents=(object(),))
    with pytest.raises(TypeError, match="expected_versions"):
        replace(plan, expected_versions=(1, 2, 3))


def test_plan_intent_and_input_hash_limits(monkeypatch, plan, now, versions) -> None:
    second = replace(plan.intents[0], intent_id="second-entry")
    monkeypatch.setattr(contracts_module, "MAX_PLAN_INTENTS", 1)
    with pytest.raises(ValueError, match="intents exceed"):
        ExecutableTradePlan.create(
            plan_id="too-many-intents",
            created_at=now,
            valid_from=plan.valid_from,
            expires_at=plan.expires_at,
            portfolio_id=plan.portfolio_id,
            instrument=plan.instrument,
            market=plan.market,
            action=plan.action,
            intents=(plan.intents[0], second),
            expected_versions=versions,
            metadata_version=plan.metadata_version,
            configuration_hash=plan.configuration_hash,
        )

    monkeypatch.setattr(contracts_module, "MAX_INPUT_HASHES", 1)
    with pytest.raises(ValueError, match="input_hashes exceed"):
        ExecutableTradePlan.create(
            plan_id="too-many-hashes",
            created_at=now,
            valid_from=plan.valid_from,
            expires_at=plan.expires_at,
            portfolio_id=plan.portfolio_id,
            instrument=plan.instrument,
            market=plan.market,
            action=plan.action,
            intents=plan.intents,
            expected_versions=versions,
            metadata_version=plan.metadata_version,
            configuration_hash=plan.configuration_hash,
            input_hashes={"a": "a" * 64, "b": "b" * 64},
        )


def test_plan_canonical_size_and_identifier_limits(monkeypatch, plan) -> None:
    monkeypatch.setattr(contracts_module, "MAX_PLAN_CANONICAL_CHARS", 16)
    with pytest.raises(ValueError, match="canonical size"):
        replace(plan, content_hash="0" * 64)

    monkeypatch.setattr(contracts_module, "MAX_IDENTIFIER_CHARS", 4)
    with pytest.raises(ValueError, match="maximum length"):
        replace(plan, plan_id="12345")

    monkeypatch.setattr(contracts_module, "MAX_IDENTIFIER_CHARS", 256)
    with pytest.raises(ValueError, match="risk_reservation_id"):
        replace(plan, risk_reservation_id=" ")


def test_venue_metadata_enums_and_flags_are_strict(metadata) -> None:
    normalized = replace(
        metadata,
        permitted_order_types=("market", "limit"),
        permitted_time_in_force=("gtc", "ioc"),
    )
    assert normalized.permitted_order_types == (
        OrderType.MARKET,
        OrderType.LIMIT,
    )

    with pytest.raises(ValueError, match="duplicates"):
        replace(
            metadata,
            permitted_order_types=(OrderType.MARKET, OrderType.MARKET),
        )
    with pytest.raises(TypeError, match="post_only_supported must be bool"):
        replace(metadata, post_only_supported=1)
