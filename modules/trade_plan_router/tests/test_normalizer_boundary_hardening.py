from __future__ import annotations

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest

from modular_strategy_trade_plan_router.enums import ReasonCode, ScalingDirection, TradeSide
from modular_strategy_trade_plan_router.errors import RouterInputError
from modular_strategy_trade_plan_router import normalizer
from modular_strategy_trade_plan_router.normalizer import DecisionNormalizer

from conftest import NOW
from test_normalizer_contracts import (
    actual_entry_payload,
    legacy_entry_payload,
    protection_payload,
    sizing_payload,
)


class BadItemsMapping(Mapping[str, object]):
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


class NonStringKeyMapping(Mapping[object, object]):
    def __getitem__(self, key: object) -> object:
        return "value"

    def __iter__(self) -> Iterator[object]:
        yield 1

    def __len__(self) -> int:
        return 1

    def items(self):
        return ((1, "value"),)


def _entry_args(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "feature_key": "entry_model.plan",
        "feature_schema_version": "2",
        "feature_created_at": NOW,
        "feature_valid_until": NOW + timedelta(minutes=5),
        "feature_id": "feature-entry",
        "source_module": "entry_model",
        "symbol": "BTCUSDT",
        "portfolio_id": "portfolio-a",
    }
    values.update(overrides)
    return values


def _protection_args(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "feature_schema_version": "1",
        "feature_created_at": NOW,
        "feature_valid_until": NOW + timedelta(minutes=5),
        "feature_id": "feature-protection",
        "symbol": "BTCUSDT",
    }
    values.update(overrides)
    return values


def _sizing_args(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "feature_schema_version": "1",
        "feature_created_at": NOW,
        "feature_valid_until": NOW + timedelta(minutes=5),
        "feature_id": "feature-sizing",
        "symbol": "BTCUSDT",
    }
    values.update(overrides)
    return values


def _scaling_args(**overrides: object) -> dict[str, object]:
    values: dict[str, object] = {
        "feature_schema_version": "1",
        "feature_created_at": NOW,
        "feature_valid_until": NOW + timedelta(minutes=5),
        "feature_id": "feature-scaling",
        "symbol": "BTCUSDT",
    }
    values.update(overrides)
    return values


def _portfolio_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "portfolio_id": "portfolio-a",
        "version": "portfolio-v1",
        "timestamp": NOW.isoformat(),
        "equity": "10000",
        "available_balance": "9000",
        "used_margin": "1000",
        "free_margin": "9000",
        "risk_already_reserved": "25",
        "available_risk_budget": "275",
        "position_state_version": "positions-v1",
        "order_state_version": "orders-v1",
        "risk_reservation_version": "risk-v1",
        "positions": [
            {
                "position_id": "position-a",
                "symbol": "BTCUSDT",
                "side": "long",
                "quantity": "2",
                "average_entry": "100",
                "version": "position-v1",
                "market": "usdt_perpetual",
                "strategy_id": "strategy-a",
                "setup_id": "setup-a",
                "initial_risk": "50",
                "remaining_risk": "40",
                "reserved_risk": "0",
                "opened_at": (NOW - timedelta(minutes=15)).isoformat(),
                "scaling_stage": 1,
            }
        ],
        "pending_orders": [
            {
                "order_intent_id": "order-a",
                "plan_id": "plan-a",
                "symbol": "BTCUSDT",
                "side": "buy",
                "order_type": "limit",
                "quantity": "1",
                "status": "pending",
                "version": "order-v1",
                "created_at": NOW.isoformat(),
                "price": "99",
                "stop_price": None,
                "reduce_only": False,
                "post_only": True,
                "reserved_risk": "25",
                "strategy_id": "strategy-a",
                "setup_id": "setup-a",
                "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            }
        ],
        "active_plan_ids": ["plan-a"],
        "instrument_locks": ["BTCUSDT"],
        "strategy_locks": ["strategy-a"],
        "portfolio_locked": False,
        "daily_loss_limit_active": False,
        "drawdown_limit_active": False,
        "emergency_state": False,
        "circuit_breaker_active": False,
        "current_drawdown": "0.01",
        "metadata": {"source": "fixture"},
    }


def _scaling_payload(*, increase: bool = False) -> dict[str, object]:
    action = "add" if increase else "reduce"
    direction = "increase" if increase else "decrease"
    reason = "risk_approved" if increase else "reduction_approved"
    risk_request_id = "risk-adjustment" if increase else None
    risk_decision = (
        {
            "schema_version": "1",
            "request_id": "risk-adjustment",
            "symbol": "BTCUSDT",
            "setup_id": "setup-a",
            "status": "approved",
            "approved": True,
            "quantity": "2",
        }
        if increase
        else None
    )
    return {
        "schema_version": "1",
        "adjustment_id": "adjustment-a",
        "position_id": "position-a",
        "symbol": "BTCUSDT",
        "side": "long",
        "action": action,
        "direction": direction,
        "approved_quantity": "2",
        "desired_price": "100",
        "risk_request_id": risk_request_id,
        "reason_code": reason,
        "approved_at": NOW.isoformat(),
        "risk_decision": risk_decision,
        "plan": {
            "schema_version": "1",
            "adjustment_id": "adjustment-a",
            "position_id": "position-a",
            "setup_id": "setup-a",
            "symbol": "BTCUSDT",
            "side": "long",
            "action": action,
            "direction": direction,
            "desired_entry_kind": "limit",
            "desired_price": "100",
            "trigger_price": None,
            "desired_quantity": "2",
            "maximum_quantity": "2",
            "stage": 1,
            "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
            "risk_request_id": risk_request_id,
        },
    }


def test_safe_datetime_add_rejects_overflow() -> None:
    with pytest.raises(RouterInputError, match="supported datetime range"):
        normalizer._safe_datetime_add(
            datetime.max.replace(tzinfo=UTC),
            timedelta(days=1),
            "expires_at",
        )


@pytest.mark.parametrize(
    ("value", "message"),
    (
        ([], "must be a mapping"),
        (BadItemsMapping(), "key/value pairs"),
        (NonStringKeyMapping(), "keys must be strings"),
        ({"": 1}, "exact non-blank"),
        ({" spaced ": 1}, "exact non-blank"),
        ({"e\u0301": 1}, "NFC"),
        ({"line\nbreak": 1}, "control characters"),
        (DuplicateItemsMapping(), "duplicate key"),
    ),
)
def test_mapping_boundary_rejects_malformed_mappings(value, message: str) -> None:
    with pytest.raises(RouterInputError, match=message):
        normalizer._mapping(value, "payload")


def test_mapping_boundary_limits_key_count_and_length(monkeypatch) -> None:
    monkeypatch.setattr(normalizer, "MAX_INPUT_MAPPING_KEYS", 1)
    with pytest.raises(RouterInputError, match="maximum key count") as error:
        normalizer._mapping({"a": 1, "b": 2}, "payload")
    assert error.value.reason is ReasonCode.INPUT_LIMIT_EXCEEDED
    monkeypatch.setattr(normalizer, "MAX_INPUT_MAPPING_KEYS", 4_096)
    monkeypatch.setattr(normalizer, "MAX_INPUT_TEXT_LENGTH", 3)
    with pytest.raises(RouterInputError, match="maximum length"):
        normalizer._mapping({"long": 1}, "payload")


def test_mapping_boundary_returns_immutable_copy() -> None:
    source = {"value": 1}
    result = normalizer._mapping(source, "payload")
    assert isinstance(result, MappingProxyType)
    source["value"] = 2
    assert result["value"] == 1


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (None, "non-blank string"),
        ("", "non-blank string"),
        ("line\nbreak", "control characters"),
    ),
)
def test_text_boundary_rejects_invalid_values(value, message: str) -> None:
    with pytest.raises(RouterInputError, match=message):
        normalizer._text(value, "field")


def test_text_boundary_defaults_normalizes_and_limits(monkeypatch) -> None:
    assert normalizer._text(None, "field", default=" default ") == "default"
    assert normalizer._text(" e\u0301 ", "field") == "é"
    assert normalizer._optional_text(None) is None
    monkeypatch.setattr(normalizer, "MAX_INPUT_TEXT_LENGTH", 3)
    with pytest.raises(RouterInputError, match="maximum length"):
        normalizer._text("long", "field")


@pytest.mark.parametrize("value", (None, "", " spaced ", "line\nbreak"))
def test_schema_version_boundary_rejects_non_exact_values(value) -> None:
    with pytest.raises(RouterInputError):
        normalizer._schema_version(value, "schema")


def test_schema_version_default_and_match() -> None:
    assert normalizer._schema_version(None, "schema", default="1") == "1"
    normalizer._require_schema_match("1", "1", "contract")
    with pytest.raises(RouterInputError, match="schema conflicts"):
        normalizer._require_schema_match("1", "2", "contract")


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (object(), "must be datetime"),
        ("not-a-date", "ISO-8601"),
        (datetime(2026, 1, 1), "timezone-aware"),
    ),
)
def test_datetime_boundary_rejects_invalid_values(value, message: str) -> None:
    with pytest.raises(RouterInputError, match=message):
        normalizer._datetime(value, "time")


def test_datetime_boundary_accepts_strings_offsets_and_optional() -> None:
    assert normalizer._datetime("2026-01-01T00:00:00Z", "time").tzinfo is UTC
    shifted = normalizer._datetime("2026-01-01T03:00:00+03:00", "time")
    assert shifted == datetime(2026, 1, 1, tzinfo=UTC)
    assert normalizer._optional_datetime(None, "time") is None


@pytest.mark.parametrize(
    ("value", "message"),
    (
        (True, "must be numeric"),
        (object(), "must be numeric"),
        ("not-a-number", "must be numeric"),
        ("NaN", "must be finite"),
    ),
)
def test_decimal_boundary_rejects_invalid_values(value, message: str) -> None:
    with pytest.raises(RouterInputError, match=message):
        normalizer._decimal(value, "amount")


def test_decimal_boundary_defaults_and_optional() -> None:
    assert normalizer._decimal(None, "amount", default="1.25") == Decimal("1.25")
    assert normalizer._decimal(2, "amount") == Decimal("2")
    assert normalizer._optional_decimal(None, "amount") is None


@pytest.mark.parametrize("value", (True, 1.5, "1"))
def test_integer_boundary_rejects_non_integer(value) -> None:
    with pytest.raises(RouterInputError, match="must be integer"):
        normalizer._integer(value, "count")


def test_integer_boundary_defaults_and_range() -> None:
    assert normalizer._integer(None, "count", default=2, minimum=1) == 2
    with pytest.raises(RouterInputError, match="must be in range"):
        normalizer._integer(0, "count", minimum=1, maximum=2)


def test_boolean_side_and_message_boundaries() -> None:
    assert normalizer._boolean(None, "flag", default=True) is True
    assert normalizer._boolean(False, "flag") is False
    with pytest.raises(RouterInputError, match="must be boolean"):
        normalizer._boolean(1, "flag")
    assert normalizer._side(TradeSide.LONG) is TradeSide.LONG
    assert normalizer._side("BUY") is TradeSide.LONG
    assert normalizer._side("short") is TradeSide.SHORT
    for value in (1, "flat"):
        with pytest.raises(RouterInputError, match="side must be"):
            normalizer._side(value)
    assert normalizer._message_text(None, "message") == ""
    assert normalizer._message_text("message", "message") == "message"
    with pytest.raises(RouterInputError, match="must be a string"):
        normalizer._message_text(1, "message")
    with pytest.raises(RouterInputError, match="maximum length"):
        normalizer._message_text("x" * 4097, "message")
    with pytest.raises(RouterInputError, match="control characters"):
        normalizer._message_text("line\nbreak", "message")


def test_tuple_strings_boundary_and_limits(monkeypatch) -> None:
    assert normalizer._tuple_strings(None, "tags") == ()
    assert normalizer._tuple_strings([" a ", "b"], "tags") == ("a", "b")
    with pytest.raises(RouterInputError, match="must be an array"):
        normalizer._tuple_strings("a", "tags")
    monkeypatch.setattr(normalizer, "MAX_INPUT_SEQUENCE_ITEMS", 1)
    with pytest.raises(RouterInputError, match="maximum item count") as error:
        normalizer._tuple_strings(["a", "b"], "tags")
    assert error.value.reason is ReasonCode.INPUT_LIMIT_EXCEEDED


def test_canonical_payload_helpers_wrap_errors() -> None:
    assert normalizer._stable_payload_id("item", {"a": 1}).startswith("item-")
    assert len(normalizer._stable_payload_hash("item", {"a": 1})) == 64
    with pytest.raises(RouterInputError, match="not canonicalizable"):
        normalizer._stable_payload_id("item", {"bad": object()})
    with pytest.raises(RouterInputError, match="not canonicalizable"):
        normalizer._stable_payload_hash("item", {"bad": object()})


@pytest.mark.parametrize(
    "kwargs",
    (
        {"strict_mode": 1},
        {"max_targets_per_plan": True},
        {"max_targets_per_plan": 0},
        {"max_targets_per_plan": normalizer.MAX_ROUTER_COLLECTION_ITEMS + 1},
        {"max_portfolio_positions": 0},
        {"max_pending_orders": 0},
    ),
)
def test_normalizer_configuration_rejects_invalid_values(kwargs) -> None:
    with pytest.raises(RouterInputError):
        DecisionNormalizer(**kwargs)


def test_entry_feature_and_schema_boundaries() -> None:
    payload = actual_entry_payload()
    router = DecisionNormalizer()
    with pytest.raises(RouterInputError, match="unsupported entry feature key"):
        router.normalize_entry(payload, **_entry_args(feature_key="entry.invalid"))
    with pytest.raises(RouterInputError, match="schema conflicts"):
        router.normalize_entry(payload, **_entry_args(feature_schema_version="1"))
    payload = dict(payload)
    payload["schema_version"] = "99"
    with pytest.raises(RouterInputError, match="unsupported entry plan schema"):
        router.normalize_entry(payload, **_entry_args(feature_schema_version="99"))


def test_entry_v2_rejects_unknown_flags_and_fingerprints() -> None:
    router = DecisionNormalizer()
    payload = actual_entry_payload()
    payload["unknown"] = True
    with pytest.raises(RouterInputError, match="unknown EntryPlan fields"):
        router.normalize_entry(payload, **_entry_args())

    payload = actual_entry_payload()
    payload["post_only"] = "false"
    with pytest.raises(RouterInputError, match="post_only must be boolean"):
        router.normalize_entry(payload, **_entry_args())

    payload = actual_entry_payload()
    payload["setup_fingerprint"] = "a" * 63
    with pytest.raises(RouterInputError, match="SHA-256"):
        router.normalize_entry(payload, **_entry_args())

    payload = actual_entry_payload()
    payload["config_fingerprint"] = "z" * 64
    with pytest.raises(RouterInputError, match="hexadecimal"):
        router.normalize_entry(payload, **_entry_args())


def test_legacy_entry_defaults_and_invalid_post_only() -> None:
    payload = legacy_entry_payload()
    for key in (
        "created_at",
        "valid_until",
        "module_version",
        "entry_kind",
        "order_type",
        "ttl_bars",
    ):
        payload.pop(key, None)
    result = DecisionNormalizer().normalize_entry(
        payload,
        **_entry_args(
            feature_key="entry_model.entry_plan",
            feature_schema_version="1",
        ),
    )
    assert result.module_version == "legacy-1"
    assert result.ttl_bars == 1
    assert result.expires_at == NOW + timedelta(minutes=5)

    payload = legacy_entry_payload()
    payload["post_only"] = 1
    with pytest.raises(RouterInputError, match="legacy post_only must be boolean"):
        DecisionNormalizer().normalize_entry(
            payload,
            **_entry_args(
                feature_key="entry_model.entry_plan",
                feature_schema_version="1",
            ),
        )


def test_entry_source_contract_hash_rejects_noncanonical_metadata() -> None:
    payload = legacy_entry_payload()
    payload["metadata"] = {"strategy_id": "strategy-a", "bad": object()}
    with pytest.raises(RouterInputError, match="not canonicalizable"):
        DecisionNormalizer().normalize_entry(
            payload,
            **_entry_args(
                feature_key="entry_model.entry_plan",
                feature_schema_version="1",
            ),
        )


def test_protection_mutation_matrix() -> None:
    router = DecisionNormalizer(max_targets_per_plan=1)
    cases = (
        ({"schema_version": "2"}, "unsupported protection schema"),
        ({"symbol": "ETHUSDT"}, "symbol conflicts"),
        ({"targets": "bad"}, "targets must be an array"),
        (
            {"targets": [{"price": "110"}, {"price": "120"}]},
            "targets exceed configured input limit",
        ),
        ({"targets": ["bad"]}, "target must be a mapping"),
        ({"created_at": (NOW - timedelta(seconds=1)).isoformat()}, "created_at conflicts"),
        ({"valid_until": (NOW + timedelta(minutes=6)).isoformat()}, "valid_until conflicts"),
        ({"max_holding_seconds": 0}, "must be in range"),
        ({"no_progress_rule": []}, "must be a mapping"),
    )
    for mutation, message in cases:
        payload = protection_payload()
        payload.update(mutation)
        with pytest.raises(RouterInputError, match=message):
            router.normalize_protection(payload, **_protection_args())


def test_protection_targets_and_fallback_take_profit() -> None:
    payload = protection_payload()
    payload.pop("targets", None)
    result = DecisionNormalizer().normalize_protection(payload, **_protection_args())
    assert len(result.targets) == 1

    payload = protection_payload()
    payload["targets"] = [
        {"price": "110", "allocation": "0.5", "target_type": "limit"},
        {"price": "120", "allocation": "0.5", "target_type": "market"},
    ]
    result = DecisionNormalizer(max_targets_per_plan=2).normalize_protection(
        payload,
        **_protection_args(),
    )
    assert len(result.targets) == 2


def test_sizing_mutation_matrix() -> None:
    router = DecisionNormalizer()
    cases = (
        ({"schema_version": "2"}, "unsupported sizing schema"),
        ({"symbol": "ETHUSDT"}, "symbol conflicts"),
        ({"status": "pending"}, "status must be approved or rejected"),
        ({"approved": "true"}, "approved flag must be boolean"),
        ({"status": "rejected", "approved": True}, "status conflicts"),
        ({"diagnostics": []}, "diagnostics must be a mapping"),
        ({"position_adjustment": []}, "position_adjustment must be a mapping"),
        ({"binding_constraints": "limit"}, "must be an array"),
        ({"message": 1}, "message must be a string"),
    )
    for mutation, message in cases:
        payload = sizing_payload()
        payload.update(mutation)
        with pytest.raises(RouterInputError, match=message):
            router.normalize_sizing(payload, **_sizing_args())


def test_sizing_rejected_path_and_generated_decision_id() -> None:
    payload = sizing_payload()
    payload.update(
        {
            "status": "rejected",
            "approved": False,
            "quantity": "0",
            "notional": "0",
            "actual_risk_amount": "0",
            "actual_risk_fraction": "0",
            "required_leverage": "0",
            "reject_reason": "risk_limit",
            "message": "rejected",
        }
    )
    result = DecisionNormalizer().normalize_sizing(
        payload,
        **_sizing_args(feature_id=None),
    )
    assert result.approved is False
    assert result.decision_id.startswith("sizing-")
    assert result.rejection_reason == "risk_limit"


def test_scaling_reduce_and_increase_paths() -> None:
    reduce_result = DecisionNormalizer().normalize_scaling(
        _scaling_payload(),
        **_scaling_args(),
    )
    assert reduce_result.direction is ScalingDirection.DECREASE
    assert reduce_result.risk_request_id is None

    increase_result = DecisionNormalizer().normalize_scaling(
        _scaling_payload(increase=True),
        **_scaling_args(),
    )
    assert increase_result.direction is ScalingDirection.INCREASE
    assert increase_result.risk_request_id == "risk-adjustment"


def test_scaling_mutation_matrix() -> None:
    cases = (
        ({"schema_version": "2"}, "unsupported scaling schema"),
        ({"symbol": "ETHUSDT"}, "symbol conflicts"),
        ({"action": "invalid"}, "unsupported scaling action"),
        ({"direction": "invalid"}, "unsupported scaling direction"),
        ({"approved_at": (NOW - timedelta(seconds=1)).isoformat()}, "approved_at conflicts"),
        ({"reason_code": "risk_approved"}, "reduction_approved"),
        ({"risk_request_id": "unexpected"}, "embedded plan risk_request_id"),
    )
    for mutation, message in cases:
        payload = _scaling_payload()
        payload.update(mutation)
        with pytest.raises(RouterInputError, match=message):
            DecisionNormalizer().normalize_scaling(payload, **_scaling_args())


def test_scaling_embedded_plan_and_risk_conflicts() -> None:
    mutations = (
        ("adjustment_id", "other", "embedded plan adjustment_id"),
        ("position_id", "other", "embedded plan position_id"),
        ("symbol", "ETHUSDT", "embedded plan symbol"),
        ("side", "short", "embedded plan side"),
        ("action", "hold", "embedded plan action"),
        ("direction", "none", "embedded plan direction"),
        ("desired_price", "101", "embedded plan desired_price"),
    )
    for key, value, message in mutations:
        payload = _scaling_payload()
        payload["plan"] = dict(payload["plan"])
        payload["plan"][key] = value
        with pytest.raises(RouterInputError, match=message):
            DecisionNormalizer().normalize_scaling(payload, **_scaling_args())

    payload = _scaling_payload(increase=True)
    payload["risk_decision"] = dict(payload["risk_decision"])
    payload["risk_decision"]["approved"] = False
    with pytest.raises(RouterInputError, match="must be approved"):
        DecisionNormalizer().normalize_scaling(payload, **_scaling_args())

    payload = _scaling_payload(increase=True)
    payload["approved_quantity"] = "3"
    with pytest.raises(RouterInputError, match="exceeds embedded plan cap"):
        DecisionNormalizer().normalize_scaling(payload, **_scaling_args())


def test_portfolio_full_path_and_aliases() -> None:
    payload = _portfolio_payload()
    result = DecisionNormalizer().normalize_portfolio(payload, feature_created_at=NOW)
    assert len(result.positions) == 1
    assert len(result.pending_orders) == 1
    assert result.positions[0].average_entry == Decimal("100")
    assert result.pending_orders[0].post_only is True
    assert result.portfolio_locked is False

    payload = _portfolio_payload()
    payload["snapshot_version"] = payload.pop("version")
    payload["snapshot_timestamp"] = payload.pop("timestamp")
    payload["positions"][0]["id"] = payload["positions"][0].pop("position_id")
    payload["positions"][0]["entry_price"] = payload["positions"][0].pop("average_entry")
    payload["pending_orders"][0]["id"] = payload["pending_orders"][0].pop("order_intent_id")
    result = DecisionNormalizer().normalize_portfolio(payload, feature_created_at=NOW)
    assert result.version == "portfolio-v1"
    assert result.positions[0].position_id == "position-a"
    assert result.pending_orders[0].order_intent_id == "order-a"


def test_portfolio_mutation_matrix() -> None:
    cases = (
        ({"schema_version": "2"}, "unsupported portfolio snapshot schema"),
        ({"positions": {}}, "positions must be an array"),
        ({"pending_orders": {}}, "pending_orders must be an array"),
        ({"portfolio_locked": 1}, "must be boolean"),
        ({"metadata": []}, "metadata must be a mapping"),
    )
    for mutation, message in cases:
        payload = _portfolio_payload()
        payload.update(mutation)
        with pytest.raises(RouterInputError, match=message):
            DecisionNormalizer().normalize_portfolio(payload, feature_created_at=NOW)


def test_portfolio_limits_and_nested_unknown_fields() -> None:
    payload = _portfolio_payload()
    with pytest.raises(RouterInputError, match="positions exceed configured") as error:
        DecisionNormalizer(max_portfolio_positions=1).normalize_portfolio(
            {**payload, "positions": payload["positions"] * 2},
            feature_created_at=NOW,
        )
    assert error.value.reason is ReasonCode.INPUT_LIMIT_EXCEEDED

    with pytest.raises(RouterInputError, match="pending orders exceed configured") as error:
        DecisionNormalizer(max_pending_orders=1).normalize_portfolio(
            {**payload, "pending_orders": payload["pending_orders"] * 2},
            feature_created_at=NOW,
        )
    assert error.value.reason is ReasonCode.INPUT_LIMIT_EXCEEDED

    payload = _portfolio_payload()
    payload["positions"][0]["unknown"] = True
    with pytest.raises(RouterInputError, match="unknown portfolio position fields"):
        DecisionNormalizer().normalize_portfolio(payload, feature_created_at=NOW)

    payload = _portfolio_payload()
    payload["pending_orders"][0]["unknown"] = True
    with pytest.raises(RouterInputError, match="unknown portfolio pending order fields"):
        DecisionNormalizer().normalize_portfolio(payload, feature_created_at=NOW)
