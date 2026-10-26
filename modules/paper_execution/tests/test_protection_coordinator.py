from __future__ import annotations

from decimal import Decimal

import pytest

from paper_execution.enums import IntentRole
from paper_execution.exceptions import OrderStateError
from paper_execution.protection import (
    DeferredProtectionLeg,
    ProtectionActivation,
    ProtectionActivationPolicy,
    ProtectionCoordinator,
)


def _coordinator(
    policy: ProtectionActivationPolicy = ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL,
) -> ProtectionCoordinator:
    coordinator = ProtectionCoordinator("entry", Decimal("1"), policy)
    coordinator.register_leg(DeferredProtectionLeg("stop", IntentRole.STOP, Decimal("1")))
    coordinator.register_leg(DeferredProtectionLeg("target", IntentRole.TARGET, Decimal("1")))
    return coordinator


def test_partial_fills_activate_protection_proportionally() -> None:
    coordinator = _coordinator()
    assert coordinator.record_entry_fill(Decimal("0.4")) == (
        ProtectionActivation("stop", IntentRole.STOP, Decimal("0.4"), Decimal("0.4")),
        ProtectionActivation("target", IntentRole.TARGET, Decimal("0.4"), Decimal("0.4")),
    )
    assert coordinator.protected_quantity == Decimal("0.4")
    assert coordinator.unprotected_quantity == Decimal("0")

    assert coordinator.record_entry_fill(Decimal("0.7")) == (
        ProtectionActivation("stop", IntentRole.STOP, Decimal("0.7"), Decimal("0.3")),
        ProtectionActivation("target", IntentRole.TARGET, Decimal("0.7"), Decimal("0.3")),
    )
    assert coordinator.record_entry_fill(Decimal("0.7")) == ()


def test_after_full_fill_policy_defers_all_legs() -> None:
    coordinator = _coordinator(ProtectionActivationPolicy.AFTER_FULL_FILL)
    assert coordinator.record_entry_fill(Decimal("0.5")) == ()
    assert coordinator.unprotected_quantity == Decimal("0.5")
    activations = coordinator.record_entry_fill(Decimal("1"))
    assert {item.client_order_id for item in activations} == {"stop", "target"}
    assert all(item.desired_total_quantity == Decimal("1") for item in activations)
    assert coordinator.unprotected_quantity == Decimal("0")


def test_fill_monotonicity_and_overfill_fail_closed() -> None:
    coordinator = _coordinator()
    coordinator.record_entry_fill(Decimal("0.6"))
    with pytest.raises(OrderStateError, match="decreased"):
        coordinator.record_entry_fill(Decimal("0.5"))
    with pytest.raises(OrderStateError, match="exceeds"):
        coordinator.record_entry_fill(Decimal("1.1"))
    assert coordinator.cumulative_entry_fill == Decimal("0.6")


def test_duplicate_leg_registration_is_idempotent_but_mutation_is_rejected() -> None:
    coordinator = ProtectionCoordinator("entry", Decimal("1"))
    leg = DeferredProtectionLeg("stop", IntentRole.STOP, Decimal("1"))
    coordinator.register_leg(leg)
    coordinator.register_leg(leg)
    with pytest.raises(OrderStateError, match="leg changed"):
        coordinator.register_leg(DeferredProtectionLeg("stop", IntentRole.STOP, Decimal("0.5")))


def test_snapshot_restore_preserves_activation_state() -> None:
    coordinator = _coordinator()
    coordinator.record_entry_fill(Decimal("0.4"))
    snapshot = coordinator.snapshot_state()
    restored = ProtectionCoordinator.from_state(snapshot)
    assert restored.snapshot_state() == snapshot
    assert restored.record_entry_fill(Decimal("0.7")) == (
        ProtectionActivation("stop", IntentRole.STOP, Decimal("0.7"), Decimal("0.3")),
        ProtectionActivation("target", IntentRole.TARGET, Decimal("0.7"), Decimal("0.3")),
    )


def test_snapshot_restore_rejects_tampering() -> None:
    snapshot = dict(_coordinator().snapshot_state())
    snapshot["cumulative_entry_fill"] = "0.5"
    with pytest.raises(ValueError, match="hash mismatch"):
        ProtectionCoordinator.from_state(snapshot)
