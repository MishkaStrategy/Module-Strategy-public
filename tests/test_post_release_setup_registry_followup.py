from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core.models import SetupEvent, SetupStatus
from modular_core.setups import (
    SetupRecord,
    SetupRegistry,
    SetupTransition,
    SetupTransitionError,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _record(
    *,
    transitions: tuple[SetupTransition, ...],
    status: SetupStatus,
) -> SetupRecord:
    return SetupRecord(
        setup_id="setup-1",
        module="probe",
        symbol="BTCUSDT",
        side=None,
        created_at=NOW,
        updated_at=NOW + timedelta(minutes=2),
        status=status,
        transitions=transitions,
    )


def test_setup_record_rejects_nonempty_initial_from_status() -> None:
    transition = SetupTransition(
        timestamp=NOW,
        from_status=SetupStatus.ARMED,
        to_status=SetupStatus.CREATED,
    )

    with pytest.raises(ValueError, match="first transition must start from None"):
        _record(transitions=(transition,), status=SetupStatus.CREATED)


def test_setup_record_rejects_discontinuous_transition_chain() -> None:
    first = SetupTransition(NOW, None, SetupStatus.CREATED)
    second = SetupTransition(
        NOW + timedelta(minutes=1),
        SetupStatus.ARMED,
        SetupStatus.CONFIRMED,
    )

    with pytest.raises(ValueError, match="transition chain is discontinuous"):
        _record(
            transitions=(first, second),
            status=SetupStatus.CONFIRMED,
        )


def test_setup_record_rejects_transition_before_created_at() -> None:
    transition = SetupTransition(
        NOW - timedelta(seconds=1),
        None,
        SetupStatus.CREATED,
    )

    with pytest.raises(ValueError, match="cannot precede record created_at"):
        _record(transitions=(transition,), status=SetupStatus.CREATED)


def test_registry_rejects_conflicting_position_link_without_mutation() -> None:
    registry = SetupRegistry()
    registry.apply(
        SetupEvent(
            module="probe",
            setup_id="setup-1",
            status=SetupStatus.CREATED,
            symbol="BTCUSDT",
        ),
        NOW,
    )
    linked = registry.link_position(
        "setup-1",
        "position-1",
        NOW + timedelta(minutes=1),
    )

    with pytest.raises(SetupTransitionError, match="already linked to position-1"):
        registry.link_position(
            "setup-1",
            "position-2",
            NOW + timedelta(minutes=2),
        )

    assert registry.get("setup-1") == linked


def test_registry_position_relink_is_idempotent() -> None:
    registry = SetupRegistry()
    registry.apply(
        SetupEvent(
            module="probe",
            setup_id="setup-1",
            status=SetupStatus.CREATED,
            symbol="BTCUSDT",
        ),
        NOW,
    )
    linked = registry.link_position(
        "setup-1",
        "position-1",
        NOW + timedelta(minutes=1),
    )

    assert (
        registry.link_position(
            "setup-1",
            "position-1",
            NOW + timedelta(minutes=2),
        )
        is linked
    )
