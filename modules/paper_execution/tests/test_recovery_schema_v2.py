from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest
from modular_core.events import InMemoryEventStore

from paper_execution import (
    DeferredProtectionLeg,
    IntentRole,
    OcoGroup,
    PaperExecutionConfig,
    PaperExecutionCoordinator,
    ProtectionCoordinator,
)
from paper_execution._utils import canonical_json, sha256_text


class StaticMetadataProvider:
    def __init__(self, metadata):
        self.metadata = metadata

    def snapshot(self, instrument, timestamp):
        assert instrument == self.metadata.instrument
        return self.metadata


def _coordinator(broker, metadata, store, run_id):
    return PaperExecutionCoordinator(
        config=PaperExecutionConfig(),
        venue=broker,
        metadata_provider=StaticMetadataProvider(metadata),
        run_id=run_id,
        event_store=store,
    )


def _rehash_root(state):
    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))
    return state


def _populate_orchestration(coordinator, entry_client_order_id):
    oco = OcoGroup("oco-1", Decimal("1"))
    oco.register("stop-1", Decimal("1"))
    oco.register("target-1", Decimal("1"))
    oco.record_fill("target-1", Decimal("0.25"))
    coordinator.orchestration_state.register_oco(oco)

    protection = ProtectionCoordinator(entry_client_order_id, Decimal("1"))
    protection.register_leg(DeferredProtectionLeg("stop-1", IntentRole.STOP, Decimal("1")))
    protection.record_entry_fill(Decimal("0.25"))
    coordinator.orchestration_state.register_protection(protection)


def test_schema_v4_round_trip_preserves_orchestration(
    plan, versions, now, broker, metadata
) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "schema-v4")
    result = source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    _populate_orchestration(source, result.client_order_ids[0])

    state = source.snapshot_state()
    assert state["schema_version"] == "4"
    recovered = _coordinator(broker, metadata, store, "schema-v4")
    recovered.restore_state(state)

    assert recovered.recovery_pending
    assert (
        recovered.orchestration_state.snapshot_state()
        == source.orchestration_state.snapshot_state()
    )
    assert recovered.snapshot_state() == state


def test_legacy_schema_v1_restores_empty_orchestration_and_upgrades(
    plan, versions, now, broker, metadata
) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "schema-v1")
    result = source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    _populate_orchestration(source, result.client_order_ids[0])

    legacy = deepcopy(dict(source.snapshot_state()))
    legacy.pop("orchestration_state")
    for order in legacy["orders"]:
        order.pop("processed_event_hashes")
        order.pop("last_event_timestamp")
    legacy["schema_version"] = "1"
    _rehash_root(legacy)

    recovered = _coordinator(broker, metadata, store, "schema-v1")
    recovered.restore_state(legacy)

    assert recovered.recovery_pending
    assert recovered.orchestration_state.oco_groups == {}
    assert recovered.orchestration_state.protection_by_entry == {}
    assert recovered.snapshot_state()["schema_version"] == "4"


def test_nested_orchestration_tampering_is_rejected_transactionally(
    plan, versions, now, broker, metadata
) -> None:
    store = InMemoryEventStore()
    source = _coordinator(broker, metadata, store, "schema-tamper")
    result = source.accept_plan(
        plan,
        now=now,
        current_index=0,
        mark_price=Decimal("100000"),
        versions=versions,
    )
    _populate_orchestration(source, result.client_order_ids[0])

    tampered = deepcopy(dict(source.snapshot_state()))
    tampered["orchestration_state"]["oco_groups"]["oco-1"]["position_quantity"] = "9"
    _rehash_root(tampered)

    target = _coordinator(broker, metadata, store, "schema-tamper")
    before = target.snapshot_state()
    with pytest.raises(ValueError, match="orchestration state hash mismatch"):
        target.restore_state(tampered)
    assert target.snapshot_state() == before
