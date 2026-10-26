from __future__ import annotations

from collections.abc import Iterator, Mapping
from decimal import Decimal
from enum import StrEnum

import pytest

from modular_strategy_trade_plan_router import RouterConfiguration
from modular_strategy_trade_plan_router import config, serialization
from modular_strategy_trade_plan_router.enums import LongShortConflictPolicy
from modular_strategy_trade_plan_router.errors import (
    RouterConfigurationError,
    RouterSerializationError,
)


class BrokenItemsMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 1

    def items(self):
        return (("broken", 1, 2),)


class DuplicateItemsMapping(Mapping[str, object]):
    def __getitem__(self, key: str) -> object:
        return 1

    def __iter__(self) -> Iterator[str]:
        yield "dup"

    def __len__(self) -> int:
        return 2

    def items(self):
        return (("dup", 1), ("dup", 2))


class ExampleEnum(StrEnum):
    VALUE = "value"


def test_configuration_decimal_boundary() -> None:
    for value in (True, object(), "bad", "NaN", "-1"):
        with pytest.raises(RouterConfigurationError):
            config._decimal(value, "value")
    assert config._decimal("1.25", "value") == Decimal("1.25")


@pytest.mark.parametrize(
    "value",
    (None, "", " spaced ", "e\u0301", "line\nbreak"),
)
def test_configuration_field_key_rejections(value) -> None:
    with pytest.raises(RouterConfigurationError):
        config._configuration_field_key(value, "fields")


def test_configuration_field_key_length(monkeypatch) -> None:
    monkeypatch.setattr(config, "MAX_CONFIGURATION_FIELD_LENGTH", 3)
    with pytest.raises(RouterConfigurationError, match="maximum length"):
        config._configuration_field_key("long", "fields")


def test_mapping_snapshot_rejections_and_immutability() -> None:
    with pytest.raises(RouterConfigurationError, match="must be a mapping"):
        config._mapping_snapshot([], "mapping", max_entries=2)
    with pytest.raises(RouterConfigurationError, match="maximum entry count"):
        config._mapping_snapshot({"a": 1, "b": 2}, "mapping", max_entries=1)
    with pytest.raises(RouterConfigurationError, match="key/value pairs"):
        config._mapping_snapshot(BrokenItemsMapping(), "mapping", max_entries=2)
    with pytest.raises(RouterConfigurationError, match="duplicate key"):
        config._mapping_snapshot(DuplicateItemsMapping(), "mapping", max_entries=3)
    source = {"a": 1}
    result = config._mapping_snapshot(source, "mapping", max_entries=2)
    source["a"] = 2
    assert result["a"] == 1


def test_configuration_key_and_priority_map_boundaries(monkeypatch) -> None:
    for value in (None, "", "line\nbreak"):
        with pytest.raises(RouterConfigurationError):
            config._configuration_key(value, "priority")
    monkeypatch.setattr(config, "MAX_CONFIGURATION_KEY_LENGTH", 3)
    with pytest.raises(RouterConfigurationError, match="maximum length"):
        config._configuration_key("long", "priority")
    monkeypatch.setattr(config, "MAX_CONFIGURATION_KEY_LENGTH", 512)

    assert dict(config._priority_map(None, "priority")) == {}
    with pytest.raises(RouterConfigurationError, match="must be a mapping"):
        config._priority_map([], "priority")
    with pytest.raises(RouterConfigurationError, match="key/value pairs"):
        config._priority_map(BrokenItemsMapping(), "priority")
    with pytest.raises(RouterConfigurationError, match="duplicate normalized"):
        config._priority_map({" a ": 1, "a": 2}, "priority")
    with pytest.raises(RouterConfigurationError, match="must be integer"):
        config._priority_map({"a": True}, "priority")
    with pytest.raises(RouterConfigurationError, match="signed 32-bit"):
        config._priority_map({"a": config.MAX_PRIORITY_ABS + 1}, "priority")
    monkeypatch.setattr(config, "MAX_PRIORITY_ENTRIES", 1)
    with pytest.raises(RouterConfigurationError, match="maximum entry count"):
        config._priority_map({"a": 1, "b": 2}, "priority")


def test_positive_int_and_enum_boundaries() -> None:
    assert config._positive_int(None, "count", 2) == 2
    for value in (True, 0, -1, "1"):
        with pytest.raises(RouterConfigurationError, match="positive integer"):
            config._positive_int(value, "count", 2)
    with pytest.raises(RouterConfigurationError, match="range"):
        config._positive_int(3, "count", 2, maximum=2)
    assert config._enum(ExampleEnum, None, "mode", "value") is ExampleEnum.VALUE
    with pytest.raises(RouterConfigurationError, match="must be a string"):
        config._enum(ExampleEnum, 1, "mode", "value")
    with pytest.raises(RouterConfigurationError, match="unsupported"):
        config._enum(ExampleEnum, "bad", "mode", "value")


def test_router_configuration_post_init_rejections() -> None:
    invalid_kwargs = (
        {"schema_version": "2"},
        {"long_short_policy": "reject_all"},
        {"strict_mode": 1},
        {"max_decision_age_seconds": 0},
        {"max_entry_candidates": config.MAX_ROUTER_COLLECTION_ITEMS + 1},
        {"max_portfolio_risk_fraction": Decimal("0")},
        {"max_portfolio_risk_fraction": Decimal("1.1")},
        {"max_symbol_notional_fraction": Decimal("0")},
        {"audit_level": "verbose"},
        {"side_priorities": {"invalid": 1}},
    )
    for kwargs in invalid_kwargs:
        with pytest.raises(RouterConfigurationError):
            RouterConfiguration(**kwargs)  # type: ignore[arg-type]


def test_router_configuration_from_mapping_rejections_and_defaults() -> None:
    assert RouterConfiguration.from_mapping(None) == RouterConfiguration()
    invalid_values = (
        {"unknown": 1},
        {"strict_mode": "yes"},
        {"schema_version": 1},
        {"audit_level": 1},
        {"long_short_policy": "invalid"},
    )
    for value in invalid_values:
        with pytest.raises(RouterConfigurationError):
            RouterConfiguration.from_mapping(value)
    parsed = RouterConfiguration.from_mapping(
        {
            "long_short_policy": LongShortConflictPolicy.FRESHEST_SIGNAL.value,
            "max_entry_candidates": 5,
            "setup_priorities": {"setup-b": 2, "setup-a": 1},
        }
    )
    assert parsed.long_short_policy is LongShortConflictPolicy.FRESHEST_SIGNAL
    assert parsed.max_entry_candidates == 5
    assert tuple(parsed.setup_priorities) == ("setup-a", "setup-b")


def test_unique_json_object_and_constant_rejection() -> None:
    assert serialization._unique_object([("a", 1)]) == {"a": 1}
    with pytest.raises(ValueError, match="duplicate"):
        serialization._unique_object([("a", 1), ("a", 2)])
    with pytest.raises(ValueError, match="non-finite"):
        serialization._reject_constant("NaN")


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (1, "must be a string"),
        ("", "non-empty"),
        ("e\u0301", "NFC"),
        ("line\nbreak", "control"),
    ),
)
def test_serialized_text_boundary(value, message: str) -> None:
    with pytest.raises(RouterSerializationError, match=message):
        serialization._require_text(value, "field")
    assert serialization._require_optional_text(None, "field") is None


def test_serialized_text_length_and_boolean() -> None:
    with pytest.raises(RouterSerializationError, match="maximum length"):
        serialization._require_text("abcd", "field", max_length=3)
    assert serialization._require_text("", "field", allow_empty=True) == ""
    assert serialization._require_bool(True, "flag") is True
    with pytest.raises(RouterSerializationError, match="boolean"):
        serialization._require_bool(1, "flag")


def test_serialized_integer_decimal_and_datetime_boundaries() -> None:
    assert serialization._require_positive_int(None, "count", allow_none=True) is None
    for value in (True, 0, -1, "1", serialization.MAX_SERIALIZED_INTEGER + 1):
        with pytest.raises(RouterSerializationError, match="positive integer"):
            serialization._require_positive_int(value, "count")

    assert serialization._require_decimal_text(None, "amount", allow_none=True) is None
    invalid_decimals = (1, "bad", "NaN", "1.0", "-1")
    for value in invalid_decimals:
        with pytest.raises(RouterSerializationError):
            serialization._require_decimal_text(value, "amount")
    with pytest.raises(RouterSerializationError, match="positive"):
        serialization._require_decimal_text("0", "amount", positive=True)
    assert serialization._require_decimal_text("1.25", "amount") == Decimal("1.25")

    with pytest.raises(RouterSerializationError, match="must be a string"):
        serialization._require_datetime_text(1, "time")
    for value in ("2026-01-01T00:00:00+00:00", "badZ"):
        with pytest.raises(RouterSerializationError, match="UTC"):
            serialization._require_datetime_text(value, "time")
    assert serialization._require_datetime_text("2026-01-01T00:00:00Z", "time").tzinfo is not None


def test_serialized_container_schema_digest_and_enum_boundaries() -> None:
    with pytest.raises(RouterSerializationError, match="JSON object"):
        serialization._require_mapping([], "mapping")
    with pytest.raises(RouterSerializationError, match="JSON array"):
        serialization._require_list({}, "rows")
    with pytest.raises(RouterSerializationError, match="missing"):
        serialization._require_exact_fields({}, frozenset({"a"}), "subject")
    with pytest.raises(RouterSerializationError, match="unknown"):
        serialization._require_exact_fields({"a": 1}, frozenset(), "subject")
    with pytest.raises(RouterSerializationError, match="SHA-256"):
        serialization._require_digest("A" * 64, "digest")
    with pytest.raises(RouterSerializationError, match="unsupported"):
        serialization._require_enum("bad", "mode", frozenset({"good"}))


def test_serialized_string_list_constraints() -> None:
    with pytest.raises(RouterSerializationError, match="cannot be empty"):
        serialization._require_string_list([], "values", non_empty=True)
    with pytest.raises(RouterSerializationError, match="unsupported"):
        serialization._require_string_list(["bad"], "values", allowed=frozenset({"good"}))
    with pytest.raises(RouterSerializationError, match="sorted and unique"):
        serialization._require_string_list(["b", "a"], "values", sorted_unique=True)
    assert serialization._require_string_list([""], "values", allow_empty_members=True) == [""]


def entry_intent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "order_type": "limit",
        "quantity": "1",
        "reference_entry_price": "100",
        "price": "100",
        "trigger_price": None,
        "limit_price": "100",
        "post_only": False,
        "ttl_bars": 1,
        "reduce_only": False,
    }
    value.update(overrides)
    return value


def test_entry_intent_geometry_matrix() -> None:
    assert serialization._validate_entry_intent(entry_intent()) == (
        Decimal("1"),
        False,
    )
    invalid = (
        entry_intent(order_type="market", price="100", limit_price=None),
        entry_intent(price=None, limit_price=None),
        entry_intent(order_type="stop_market", price=None, limit_price=None),
        entry_intent(order_type="stop_limit", price=None, limit_price=None),
    )
    for value in invalid:
        with pytest.raises(RouterSerializationError):
            serialization._validate_entry_intent(value)


def target(price: str = "110", allocation: str = "1") -> dict[str, str]:
    return {"price": price, "allocation": allocation, "target_type": "limit"}


def protection_intent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "stop_loss": "95",
        "targets": [target()],
        "max_holding_seconds": None,
        "no_progress_rule": {},
        "invalidation_rule": {},
    }
    value.update(overrides)
    return value


def test_target_and_protection_intent_boundaries() -> None:
    with pytest.raises(RouterSerializationError, match="allocation"):
        serialization._validate_target(target(allocation="1.1"), 0)
    with pytest.raises(RouterSerializationError, match="cannot be empty"):
        serialization._validate_protection_intent(protection_intent(targets=[]))
    with pytest.raises(RouterSerializationError, match="allocations exceed"):
        serialization._validate_protection_intent(
            protection_intent(targets=[target(allocation="0.6"), target(allocation="0.6")])
        )
    serialization._validate_protection_intent(protection_intent())


def sizing_intent(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "quantity": "1",
        "notional": "100",
        "risk_amount": "1",
        "risk_fraction": "0.01",
        "leverage": "1",
        "binding_constraints": [],
    }
    value.update(overrides)
    return value


def test_sizing_cancellation_precondition_and_audit_boundaries() -> None:
    with pytest.raises(RouterSerializationError, match="cannot exceed 1"):
        serialization._validate_sizing_intent(sizing_intent(risk_fraction="1.1"))
    with pytest.raises(RouterSerializationError, match="sorted and unique"):
        serialization._validate_sizing_intent(sizing_intent(binding_constraints=["b", "a"]))

    cancellation = {
        "order_intent_id": "order-b",
        "expected_order_version": "v1",
        "reason": "replace",
    }
    other = {**cancellation, "order_intent_id": "order-a"}
    with pytest.raises(RouterSerializationError, match="sorted and unique"):
        serialization._validate_cancellation_intents([cancellation, other])

    precondition = {"kind": "portfolio_version", "expected": "v1"}
    with pytest.raises(RouterSerializationError, match="duplicate"):
        serialization._validate_preconditions([precondition, precondition])

    audit = {
        "sequence": 2,
        "stage": "stage",
        "outcome": "accepted",
        "reason_code": None,
        "candidate_id": None,
        "detail": {},
    }
    with pytest.raises(RouterSerializationError, match="contiguous"):
        serialization._validate_audit_trace([audit], "audit_trace")


def test_rejected_candidate_collection_constraints() -> None:
    first = {
        "candidate_id": "b",
        "setup_id": "setup-b",
        "reason_codes": ["stale_decision"],
        "detail": "",
    }
    second = {**first, "candidate_id": "a"}
    with pytest.raises(RouterSerializationError, match="sorted and unique"):
        serialization._validate_rejected_candidates([first, second], "rejected_candidates")
    invalid = {**first, "reason_codes": ["invalid"]}
    with pytest.raises(RouterSerializationError, match="unsupported"):
        serialization._validate_rejected_candidates([invalid], "rejected_candidates")


def test_serializer_constructor_dump_decode_and_json_boundaries() -> None:
    for value in (True, 0, serialization.MAX_CONFIGURED_PAYLOAD_BYTES + 1):
        with pytest.raises(ValueError):
            serialization.TradePlanSerializer(value)  # type: ignore[arg-type]
    serializer = serialization.TradePlanSerializer(max_payload_bytes=64)
    with pytest.raises(RouterSerializationError, match="ExecutableTradePlan"):
        serializer.dumps_plan(object())  # type: ignore[arg-type]
    with pytest.raises(RouterSerializationError, match="RoutingResult"):
        serializer.dumps_result(object())  # type: ignore[arg-type]
    with pytest.raises(RouterSerializationError, match="str or bytes"):
        serializer._decode(1)  # type: ignore[arg-type]
    with pytest.raises(RouterSerializationError, match="valid UTF-8"):
        serializer._decode(b"\xff")
    with pytest.raises(RouterSerializationError, match="exceeds"):
        serializer._decode(b"x" * 65)
    with pytest.raises(RouterSerializationError, match="invalid JSON"):
        serializer._load_mapping('{"a":1,"a":2}')
    with pytest.raises(RouterSerializationError, match="invalid JSON"):
        serializer._load_mapping('{"a":NaN}')
    with pytest.raises(RouterSerializationError, match="root"):
        serializer._load_mapping("[]")
    with pytest.raises(RouterSerializationError, match="cannot serialize"):
        serializer._bounded_dump({"bad": object()})
    with pytest.raises(RouterSerializationError, match="exceeds"):
        serializer._bounded_dump({"value": "x" * 100})
