from __future__ import annotations

from decimal import Decimal

import pytest

from paper_execution.enums import IntentRole
from paper_execution.oco import OcoGroup
from paper_execution.orchestration_state import ExecutionOrchestrationState
from paper_execution.protection import DeferredProtectionLeg, ProtectionCoordinator


def _state() -> ExecutionOrchestrationState:
    oco = OcoGroup("oco-1", Decimal("1"))
    oco.register("stop", Decimal("1"))
    oco.register("target", Decimal("1"))
    oco.record_fill("target", Decimal("0.25"))

    protection = ProtectionCoordinator("entry", Decimal("1"))
    protection.register_leg(DeferredProtectionLeg("stop", IntentRole.STOP, Decimal("1")))
    protection.record_entry_fill(Decimal("0.25"))

    state = ExecutionOrchestrationState()
    state.register_oco(oco)
    state.register_protection(protection)
    return state


def test_orchestration_state_round_trip_is_deterministic() -> None:
    state = _state()
    snapshot = state.snapshot_state()
    restored = ExecutionOrchestrationState.from_state(snapshot)
    assert restored.snapshot_state() == snapshot
    assert restored.oco_groups["oco-1"].remaining_position_quantity == Decimal("0.75")
    assert restored.protection_by_entry["entry"].protected_quantity == Decimal("0.25")


def test_duplicate_registration_is_idempotent_but_conflict_is_rejected() -> None:
    state = _state()
    state.register_oco(state.oco_groups["oco-1"])
    state.register_protection(state.protection_by_entry["entry"])

    conflicting = OcoGroup("oco-1", Decimal("2"))
    with pytest.raises(ValueError, match="OCO group state changed"):
        state.register_oco(conflicting)

    conflicting_protection = ProtectionCoordinator("entry", Decimal("2"))
    with pytest.raises(ValueError, match="protection state changed"):
        state.register_protection(conflicting_protection)


def test_orchestration_state_rejects_tampering_and_key_mismatch() -> None:
    snapshot = dict(_state().snapshot_state())
    tampered = dict(snapshot)
    tampered["schema_version"] = "2"
    with pytest.raises(ValueError):
        ExecutionOrchestrationState.from_state(tampered)

    nested = dict(snapshot)
    nested_oco = dict(nested["oco_groups"])
    nested_oco["wrong-key"] = nested_oco.pop("oco-1")
    nested["oco_groups"] = nested_oco
    # Recompute only the outer payload to prove nested identity validation is independent.
    from paper_execution._utils import canonical_json, sha256_text

    payload = {key: value for key, value in nested.items() if key != "state_hash"}
    nested["state_hash"] = sha256_text(canonical_json(payload))
    with pytest.raises(ValueError, match="key mismatch"):
        ExecutionOrchestrationState.from_state(nested)
