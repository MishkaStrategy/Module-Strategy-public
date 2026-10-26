from __future__ import annotations

from copy import deepcopy
from decimal import Decimal

import pytest

from paper_execution._utils import canonical_json, sha256_text
from paper_execution.enums import IntentRole
from paper_execution.exceptions import OrderStateError
from paper_execution.protection import (
    DeferredProtectionLeg,
    ProtectionActivation,
    ProtectionActivationPolicy,
    ProtectionCoordinator,
)


def _rehash(state: dict[str, object]) -> dict[str, object]:
    body = {key: value for key, value in state.items() if key != "state_hash"}
    state["state_hash"] = sha256_text(canonical_json(body))
    return state


def test_per_leg_policies_activate_stop_before_terminal_target() -> None:
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

    assert coordinator.record_entry_state(
        Decimal("0.4"),
        entry_terminal=False,
    ) == (
        ProtectionActivation(
            "stop",
            IntentRole.STOP,
            Decimal("0.4"),
            Decimal("0.4"),
            Decimal("90"),
        ),
    )
    assert coordinator.active_trigger_price(IntentRole.STOP) == Decimal("90")
    assert coordinator.active_trigger_price(IntentRole.TARGET) is None

    assert coordinator.record_entry_state(
        Decimal("0.4"),
        entry_terminal=True,
    ) == (
        ProtectionActivation(
            "target",
            IntentRole.TARGET,
            Decimal("0.4"),
            Decimal("0.4"),
            Decimal("120"),
        ),
    )
    assert coordinator.active_trigger_price(IntentRole.TARGET) == Decimal("120")
    assert coordinator.entry_terminal


def test_schema_v5_round_trip_preserves_prices_policies_and_terminal_state() -> None:
    coordinator = ProtectionCoordinator("entry", Decimal("1"))
    coordinator.register_leg(
        DeferredProtectionLeg(
            "stop",
            IntentRole.STOP,
            Decimal("1"),
            trigger_price=Decimal("90.5"),
            activation_policy=ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL,
        )
    )
    coordinator.record_entry_state(Decimal("0.25"), entry_terminal=True)

    snapshot = coordinator.snapshot_state()
    assert snapshot["schema_version"] == "5"
    restored = ProtectionCoordinator.from_state(snapshot)
    assert restored.snapshot_state() == snapshot
    assert restored.legs["stop"].trigger_price == Decimal("90.5")
    assert (
        restored.legs["stop"].activation_policy
        is ProtectionActivationPolicy.AFTER_FIRST_FILL_PROPORTIONAL
    )
    assert restored.entry_terminal


def test_legacy_schema_v1_migrates_without_inventing_prices_or_terminal_state() -> None:
    coordinator = ProtectionCoordinator("entry", Decimal("1"))
    coordinator.register_leg(
        DeferredProtectionLeg(
            "stop",
            IntentRole.STOP,
            Decimal("1"),
            trigger_price=Decimal("90"),
        )
    )
    coordinator.record_entry_fill(Decimal("0.25"))
    legacy = deepcopy(dict(coordinator.snapshot_state()))
    legacy["schema_version"] = "1"
    legacy.pop("entry_terminal")
    legacy.pop("baseline_position_quantity")
    for raw_leg in legacy["legs"].values():
        raw_leg.pop("trigger_price")
        raw_leg.pop("activation_policy")
    _rehash(legacy)

    restored = ProtectionCoordinator.from_state(legacy)
    assert restored.legs["stop"].trigger_price is None
    assert restored.legs["stop"].activation_policy is None
    assert not restored.entry_terminal
    assert restored.snapshot_state()["schema_version"] == "5"


def test_legacy_schema_v2_infers_terminal_only_from_full_fill() -> None:
    coordinator = ProtectionCoordinator("entry", Decimal("1"))
    coordinator.register_leg(
        DeferredProtectionLeg(
            "stop",
            IntentRole.STOP,
            Decimal("1"),
            trigger_price=Decimal("90"),
        )
    )
    coordinator.record_entry_fill(Decimal("1"))
    legacy = deepcopy(dict(coordinator.snapshot_state()))
    legacy["schema_version"] = "2"
    legacy.pop("entry_terminal")
    legacy.pop("baseline_position_quantity")
    _rehash(legacy)

    restored = ProtectionCoordinator.from_state(legacy)
    assert restored.entry_terminal
    assert restored.snapshot_state()["schema_version"] == "5"


def test_ambiguous_active_prices_fail_closed() -> None:
    coordinator = ProtectionCoordinator("entry", Decimal("1"))
    coordinator.register_leg(
        DeferredProtectionLeg(
            "stop-a",
            IntentRole.STOP,
            Decimal("1"),
            trigger_price=Decimal("90"),
        )
    )
    coordinator.register_leg(
        DeferredProtectionLeg(
            "stop-b",
            IntentRole.STOP,
            Decimal("1"),
            trigger_price=Decimal("91"),
        )
    )
    coordinator.record_entry_fill(Decimal("0.5"))

    with pytest.raises(OrderStateError, match="ambiguous"):
        coordinator.active_trigger_price(IntentRole.STOP)
