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


def _coordinator() -> ProtectionCoordinator:
    coordinator = ProtectionCoordinator("entry", Decimal("1"))
    coordinator.register_leg(
        DeferredProtectionLeg(
            "stop",
            IntentRole.STOP,
            Decimal("1"),
            trigger_price=Decimal("90"),
            activation_policy=ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL,
        )
    )
    coordinator.register_leg(
        DeferredProtectionLeg(
            "target",
            IntentRole.TARGET,
            Decimal("1"),
            trigger_price=Decimal("120"),
            activation_policy=ProtectionActivationPolicy.AFTER_ENTRY_TERMINAL,
        )
    )
    return coordinator


def test_observation_does_not_claim_external_activation() -> None:
    coordinator = _coordinator()
    assert coordinator.observe_entry_state(
        Decimal("0.4"),
        entry_terminal=False,
    )

    pending = coordinator.pending_activations()
    assert pending == (
        ProtectionActivation(
            "stop",
            IntentRole.STOP,
            Decimal("0.4"),
            Decimal("0.4"),
            Decimal("90"),
        ),
    )
    assert coordinator.cumulative_entry_fill == Decimal("0.4")
    assert coordinator.protected_quantity == Decimal("0")
    assert coordinator.unprotected_quantity == Decimal("0.4")
    assert coordinator.desired_trigger_price(IntentRole.STOP) == Decimal("90")
    assert coordinator.active_trigger_price(IntentRole.STOP) is None
    assert coordinator.pending_activations() == pending


def test_commit_requires_exact_current_pending_set() -> None:
    coordinator = _coordinator()
    coordinator.observe_entry_state(Decimal("0.4"), entry_terminal=False)
    pending = coordinator.pending_activations()

    with pytest.raises(OrderStateError, match="stale or incomplete"):
        coordinator.commit_activations(())
    assert coordinator.protected_quantity == Decimal("0")

    coordinator.commit_activations(pending)
    assert coordinator.protected_quantity == Decimal("0.4")
    assert coordinator.pending_activations() == ()
    assert coordinator.active_trigger_price(IntentRole.STOP) == Decimal("90")

    with pytest.raises(OrderStateError, match="stale or incomplete"):
        coordinator.commit_activations(pending)


def test_pending_activation_survives_snapshot_and_restart() -> None:
    coordinator = _coordinator()
    coordinator.observe_entry_state(Decimal("0.4"), entry_terminal=False)
    snapshot = coordinator.snapshot_state()

    restored = ProtectionCoordinator.from_state(snapshot)
    assert restored.snapshot_state() == snapshot
    assert restored.protected_quantity == Decimal("0")
    assert restored.cumulative_entry_fill == Decimal("0.4")
    assert restored.pending_activations() == coordinator.pending_activations()


def test_terminal_observation_adds_target_without_recommitting_stop() -> None:
    coordinator = _coordinator()
    coordinator.observe_entry_state(Decimal("0.4"), entry_terminal=False)
    coordinator.commit_activations(coordinator.pending_activations())

    coordinator.observe_entry_state(Decimal("0.4"), entry_terminal=True)
    assert coordinator.pending_activations() == (
        ProtectionActivation(
            "target",
            IntentRole.TARGET,
            Decimal("0.4"),
            Decimal("0.4"),
            Decimal("120"),
        ),
    )
    assert coordinator.protected_quantity == Decimal("0.4")
    assert coordinator.active_trigger_price(IntentRole.TARGET) is None
    assert coordinator.desired_trigger_price(IntentRole.TARGET) == Decimal("120")
