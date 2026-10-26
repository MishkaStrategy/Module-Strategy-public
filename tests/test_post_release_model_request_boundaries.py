from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from modular_core.models import (
    OrderRequest,
    OrderType,
    PositionSide,
    SetupEvent,
    SetupStatus,
    Side,
    Signal,
    TimeInForce,
)


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_signal_detaches_nested_metadata() -> None:
    metadata = {"nested": {"values": [1, 2]}}

    signal = Signal("probe", Side.BUY, "entry", metadata=metadata)
    metadata["nested"]["values"].append(3)  # type: ignore[index,union-attr]

    assert isinstance(signal.metadata, MappingProxyType)
    assert signal.metadata["nested"]["values"] == (1, 2)  # type: ignore[index]


@pytest.mark.parametrize("value", ["buy", True, object()])
def test_signal_requires_side_enum(value: object) -> None:
    with pytest.raises(TypeError, match="signal side must be Side"):
        Signal("probe", value, "entry")  # type: ignore[arg-type]


def test_signal_requires_metadata_mapping() -> None:
    with pytest.raises(TypeError, match="signal metadata must be a mapping"):
        Signal("probe", Side.BUY, "entry", metadata=[("value", 1)])  # type: ignore[arg-type]


def test_setup_event_detaches_nested_metadata() -> None:
    metadata = {"nested": {"values": [1]}}

    event = SetupEvent(
        module="probe",
        setup_id="setup-1",
        status=SetupStatus.CREATED,
        metadata=metadata,
        symbol="BTCUSDT",
        side=Side.BUY,
    )
    metadata["nested"]["values"].append(2)  # type: ignore[index,union-attr]

    assert isinstance(event.metadata, MappingProxyType)
    assert event.metadata["nested"]["values"] == (1,)  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("status", "created", "setup status must be SetupStatus"),
        ("reason", 1, "setup reason must be str"),
        ("symbol", " ", "setup symbol is required"),
        ("side", "buy", "setup side must be Side or None"),
        ("parent_setup_id", " ", "parent_setup_id is required"),
        ("metadata", [], "setup metadata must be a mapping"),
    ],
)
def test_setup_event_rejects_invalid_boundary_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    values: dict[str, object] = {
        "module": "probe",
        "setup_id": "setup-1",
        "status": SetupStatus.CREATED,
    }
    values[field] = value

    with pytest.raises((TypeError, ValueError), match=message):
        SetupEvent(**values)  # type: ignore[arg-type]


def _order(**overrides: object) -> OrderRequest:
    values: dict[str, object] = {
        "side": Side.BUY,
        "order_type": OrderType.MARKET,
        "quantity": 1.0,
    }
    values.update(overrides)
    return OrderRequest(**values)  # type: ignore[arg-type]


def test_order_request_detaches_nested_metadata() -> None:
    metadata = {"nested": {"values": [1, 2]}}

    request = _order(metadata=metadata)
    metadata["nested"]["values"].append(3)  # type: ignore[index,union-attr]

    assert isinstance(request.metadata, MappingProxyType)
    assert request.metadata["nested"]["values"] == (1, 2)  # type: ignore[index]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("side", "buy", "order side must be Side"),
        ("order_type", "market", "order_type must be OrderType"),
        ("time_in_force", "gtc", "time_in_force must be TimeInForce"),
        ("position_side", "long", "position_side must be PositionSide or None"),
        ("post_only", 1, "post_only must be bool"),
        ("reduce_only", 0, "reduce_only must be bool"),
        ("close_position", "no", "close_position must be bool"),
        ("tag", 1, "tag must be str"),
        ("metadata", [], "order metadata must be a mapping"),
        ("setup_id", 1, "setup_id must be str"),
        ("client_order_id", 1, "client_order_id must be str"),
    ],
)
def test_order_request_rejects_invalid_boundary_fields(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _order(**{field: value})


@pytest.mark.parametrize("value", [True, 1.5, "2"])
def test_order_request_ttl_requires_exact_integer(value: object) -> None:
    with pytest.raises(TypeError, match="ttl_bars must be int or None"):
        _order(ttl_bars=value)


def test_order_request_rejects_nonpositive_ttl() -> None:
    with pytest.raises(ValueError, match="ttl_bars must be >= 1"):
        _order(ttl_bars=0)


def test_order_request_preserves_valid_typed_execution_fields() -> None:
    offset = timezone(timedelta(hours=3))
    good_till = (NOW + timedelta(minutes=10)).astimezone(offset)

    request = OrderRequest(
        side=Side.SELL,
        order_type=OrderType.LIMIT,
        quantity=1.0,
        price=100.0,
        time_in_force=TimeInForce.GTD,
        good_till_time=good_till,
        post_only=True,
        reduce_only=True,
        position_side=PositionSide.SHORT,
        setup_id="setup-1",
        client_order_id="client-1",
        ttl_bars=4,
    )

    assert request.good_till_time == NOW + timedelta(minutes=10)
    assert request.position_side is PositionSide.SHORT
    assert request.ttl_bars == 4


def test_replace_prices_preserves_immutable_metadata() -> None:
    request = _order(metadata={"nested": {"values": [1]}})

    replaced = request.replace_prices(
        price=None,
        trigger_price=None,
        stop_loss=90.0,
        take_profit=110.0,
        quantity=2.0,
    )

    assert replaced.metadata["nested"]["values"] == (1,)  # type: ignore[index]
    assert replaced.quantity == 2.0
