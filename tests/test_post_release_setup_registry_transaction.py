from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core.models import SetupEvent, SetupStatus
from modular_core.setups import SetupRegistry, SetupTransitionError


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_child_setup_backwards_timestamp_leaves_registry_unchanged() -> None:
    registry = SetupRegistry()
    parent = registry.apply(
        SetupEvent(
            module="probe",
            setup_id="parent",
            status=SetupStatus.CREATED,
            symbol="BTCUSDT",
        ),
        NOW,
    )
    parent = registry.apply(
        SetupEvent(
            module="probe",
            setup_id="parent",
            status=SetupStatus.ARMED,
            symbol="BTCUSDT",
        ),
        NOW + timedelta(minutes=1),
    )

    with pytest.raises(
        SetupTransitionError,
        match="cannot precede parent updated_at",
    ):
        registry.apply(
            SetupEvent(
                module="probe",
                setup_id="child",
                status=SetupStatus.CREATED,
                symbol="BTCUSDT",
                parent_setup_id="parent",
            ),
            NOW + timedelta(seconds=30),
        )

    assert registry.get("parent") == parent
    assert registry.get("child") is None
    assert registry.all() == (parent,)
