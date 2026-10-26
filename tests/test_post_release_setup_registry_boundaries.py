from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from modular_core.models import SetupEvent, SetupStatus
from modular_core.setups import (
    SetupRecord,
    SetupRegistry,
    SetupTransition,
    SetupTransitionError,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _event(
    status: SetupStatus = SetupStatus.CREATED,
    **overrides: object,
) -> SetupEvent:
    values: dict[str, object] = {
        "module": "probe",
        "setup_id": "setup-1",
        "status": status,
        "metadata": {},
        "symbol": "BTCUSDT",
    }
    values.update(overrides)
    return SetupEvent(**values)  # type: ignore[arg-type]


def _record(**overrides: object) -> SetupRecord:
    transition = SetupTransition(
        timestamp=NOW,
        from_status=None,
        to_status=SetupStatus.CREATED,
    )
    values: dict[str, object] = {
        "setup_id": "setup-1",
        "module": "probe",
        "symbol": "BTCUSDT",
        "side": None,
        "created_at": NOW,
        "updated_at": NOW,
        "status": SetupStatus.CREATED,
        "metadata": {},
        "transitions": (transition,),
    }
    values.update(overrides)
    return SetupRecord(**values)  # type: ignore[arg-type]


def test_registry_detaches_and_deep_freezes_setup_metadata() -> None:
    source = {"nested": {"values": [1, 2]}}
    registry = SetupRegistry()

    record = registry.apply(_event(metadata=source), NOW)
    source["nested"]["values"].append(3)  # type: ignore[index,union-attr]

    assert isinstance(record.metadata, MappingProxyType)
    assert record.metadata["nested"]["values"] == (1, 2)  # type: ignore[index]


def test_registry_metadata_merge_remains_detached() -> None:
    registry = SetupRegistry()
    registry.apply(_event(metadata={"first": {"value": 1}}), NOW)
    update = {"second": {"values": [2]}}

    record = registry.apply(
        _event(SetupStatus.ARMED, metadata=update),
        NOW + timedelta(minutes=1),
    )
    update["second"]["values"].append(3)  # type: ignore[index,union-attr]

    assert record.metadata["first"]["value"] == 1  # type: ignore[index]
    assert record.metadata["second"]["values"] == (2,)  # type: ignore[index]


def test_setup_record_normalizes_timestamps_to_utc() -> None:
    offset = timezone(timedelta(hours=3))
    transition = SetupTransition(
        timestamp=NOW.astimezone(offset),
        from_status=None,
        to_status=SetupStatus.CREATED,
    )

    record = _record(
        created_at=NOW.astimezone(offset),
        updated_at=NOW.astimezone(offset),
        transitions=(transition,),
    )

    assert record.created_at == NOW
    assert record.updated_at == NOW
    assert record.transitions[0].timestamp == NOW


def test_registry_rejects_backwards_event_without_mutation() -> None:
    registry = SetupRegistry()
    current = registry.apply(_event(), NOW)

    with pytest.raises(SetupTransitionError, match="cannot move backwards"):
        registry.apply(
            _event(SetupStatus.ARMED),
            NOW - timedelta(seconds=1),
        )

    assert registry.get("setup-1") == current


@pytest.mark.parametrize("operation", ["order", "position"])
def test_registry_links_reject_backwards_timestamp_without_mutation(
    operation: str,
) -> None:
    registry = SetupRegistry()
    current = registry.apply(_event(), NOW)

    with pytest.raises(SetupTransitionError, match="cannot move backwards"):
        if operation == "order":
            registry.link_order(
                "setup-1",
                "order-1",
                NOW - timedelta(seconds=1),
            )
        else:
            registry.link_position(
                "setup-1",
                "position-1",
                NOW - timedelta(seconds=1),
            )

    assert registry.get("setup-1") == current


@pytest.mark.parametrize(
    ("operation", "arguments", "message"),
    [
        ("apply", (object(), NOW), "event must be SetupEvent"),
        ("get", (" ",), "setup_id is required"),
        ("link_order", ("setup-1", " ", NOW), "order_id is required"),
        (
            "link_position",
            ("setup-1", " ", NOW),
            "position_id is required",
        ),
    ],
)
def test_registry_rejects_invalid_public_boundary_arguments(
    operation: str,
    arguments: tuple[object, ...],
    message: str,
) -> None:
    registry = SetupRegistry()
    if operation.startswith("link"):
        registry.apply(_event(), NOW)

    with pytest.raises((TypeError, ValueError), match=message):
        getattr(registry, operation)(*arguments)


def test_setup_record_rejects_mutable_sequence_identity_duplicates() -> None:
    with pytest.raises(ValueError, match="order_ids cannot contain duplicates"):
        _record(order_ids=["order-1", "order-1"])


def test_setup_record_rejects_status_transition_mismatch() -> None:
    with pytest.raises(ValueError, match="status must match the final transition"):
        _record(status=SetupStatus.ARMED)


def test_setup_record_rejects_transition_after_updated_at() -> None:
    transition = SetupTransition(
        timestamp=NOW + timedelta(seconds=1),
        from_status=None,
        to_status=SetupStatus.CREATED,
    )

    with pytest.raises(ValueError, match="cannot follow record updated_at"):
        _record(transitions=(transition,))


def test_setup_record_rejects_unordered_transitions() -> None:
    first = SetupTransition(
        timestamp=NOW + timedelta(seconds=1),
        from_status=None,
        to_status=SetupStatus.CREATED,
    )
    second = SetupTransition(
        timestamp=NOW,
        from_status=SetupStatus.CREATED,
        to_status=SetupStatus.ARMED,
    )

    with pytest.raises(ValueError, match="ordered by timestamp"):
        _record(
            updated_at=NOW + timedelta(seconds=1),
            status=SetupStatus.ARMED,
            transitions=(first, second),
        )


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("from_status", "created", "from_status must be SetupStatus or None"),
        ("to_status", "created", "to_status must be SetupStatus"),
        ("reason", 1, "reason must be str"),
        ("event_id", " ", "event_id is required"),
    ],
)
def test_setup_transition_rejects_invalid_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    values: dict[str, object] = {
        "timestamp": NOW,
        "from_status": None,
        "to_status": SetupStatus.CREATED,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        SetupTransition(**values)  # type: ignore[arg-type]


def test_setup_record_copy_isolated_from_nested_metadata_source() -> None:
    source = {"nested": {"values": [1]}}
    record = _record(metadata=source)
    clone = replace(record)

    source["nested"]["values"].append(2)  # type: ignore[index,union-attr]

    assert clone.metadata["nested"]["values"] == (1,)  # type: ignore[index]
