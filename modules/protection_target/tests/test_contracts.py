from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from conftest import make_bar, make_entry_feature, make_structural_feature
from modular_core import Feature

from modular_strategy_protection_target import ProtectionTargetContractError
from modular_strategy_protection_target.contracts import (
    aware_datetime,
    canonical_fingerprint,
    decimal_value,
    feature_process_key,
    parse_entry_plan_feature,
    select_structural_feature,
)


@pytest.mark.parametrize("value", [True, object(), "not-number", "NaN", "Infinity", "0", "-1"])
def test_decimal_value_rejects_invalid_positive(value: object) -> None:
    with pytest.raises(ProtectionTargetContractError):
        decimal_value(value, "value")


def test_decimal_value_allows_zero_when_requested() -> None:
    assert decimal_value("0", "value", positive=False) == 0


def test_decimal_value_limits_digits_and_exponent() -> None:
    with pytest.raises(ProtectionTargetContractError, match="significant"):
        decimal_value("1" * 257, "value")
    with pytest.raises(ProtectionTargetContractError, match="exponent"):
        decimal_value("1E+1000001", "value")


@pytest.mark.parametrize("value", ["bad", 1, datetime(2026, 1, 1)])
def test_aware_datetime_rejects_invalid(value: object) -> None:
    with pytest.raises(ProtectionTargetContractError):
        aware_datetime(value, "timestamp")


def test_aware_datetime_normalizes_utc() -> None:
    assert aware_datetime("2026-01-01T03:00:00+03:00", "timestamp") == datetime(
        2026, 1, 1, tzinfo=UTC
    )


def test_canonical_fingerprint_is_stable() -> None:
    first = {"b": Decimal("1.00"), "a": [datetime(2026, 1, 1, tzinfo=UTC)]}
    second = {"a": (datetime(2026, 1, 1, tzinfo=UTC),), "b": Decimal("1")}
    assert canonical_fingerprint(first) == canonical_fingerprint(second)


@pytest.mark.parametrize(
    "payload",
    [
        {1: "bad"},
        {"a": object()},
        {"a": {1, 2}},
        {"a": datetime(2026, 1, 1)},
        {"a": float("nan")},
        {"a": float("inf")},
    ],
)
def test_canonical_fingerprint_rejects_nondeterministic_types(payload: object) -> None:
    with pytest.raises(ProtectionTargetContractError):
        canonical_fingerprint(payload)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"schema_version": "2"}, "schema_version"),
        ({"entry_plan_id": "bad id"}, "entry_plan_id"),
        ({"setup_id": ""}, "setup_id"),
        ({"side": "flat"}, "side"),
        ({"symbol": "ETHUSDT"}, "symbol"),
        ({"timeframe": "1h"}, "timeframe"),
        ({"created_at": "2026-01-01T00:00:00+00:00"}, "created_at"),
    ],
)
def test_entry_payload_contract_edges(change: dict[str, object], message: str) -> None:
    bar = make_bar()
    feature = make_entry_feature(bar, extra=change)
    with pytest.raises(ProtectionTargetContractError, match=message):
        parse_entry_plan_feature(
            feature, expected_symbol=bar.symbol, expected_timeframe=bar.timeframe
        )


def test_outer_feature_contract_edges() -> None:
    bar = make_bar()
    valid = make_entry_feature(bar)
    wrong_schema = Feature(
        key=valid.key,
        source_module=valid.source_module,
        created_at=valid.created_at,
        value=valid.value,
        symbol=valid.symbol,
        timeframe=valid.timeframe,
        valid_until=valid.valid_until,
        schema_version="2",
        feature_id="wrong-schema",
    )
    with pytest.raises(ProtectionTargetContractError, match="schema_version"):
        parse_entry_plan_feature(
            wrong_schema, expected_symbol=bar.symbol, expected_timeframe=bar.timeframe
        )
    nonmapping = Feature(
        key=valid.key,
        source_module=valid.source_module,
        created_at=valid.created_at,
        value="bad",
        symbol=valid.symbol,
        timeframe=valid.timeframe,
        feature_id="nonmapping",
    )
    with pytest.raises(ProtectionTargetContractError, match="mapping"):
        parse_entry_plan_feature(
            nonmapping, expected_symbol=bar.symbol, expected_timeframe=bar.timeframe
        )


def test_entry_plan_schema_v2_is_accepted_for_current_entry_model_contract() -> None:
    bar = make_bar()
    expires_at = bar.close_time + timedelta(minutes=15)
    feature = Feature(
        key="entry_model.plan",
        source_module="entry_model",
        created_at=bar.close_time,
        value={
            "schema_version": "2",
            "plan_id": "entry-v2",
            "module_version": "0.2.0",
            "setup_id": "setup-1",
            "symbol": bar.symbol,
            "side": "buy",
            "created_at": bar.close_time.isoformat(),
            "expires_at": expires_at.isoformat(),
            "price": "100",
            "metadata": {},
        },
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        valid_until=expires_at,
        schema_version="2",
        feature_id="entry-v2",
        invalidate_on_gap=True,
    )

    parsed = parse_entry_plan_feature(
        feature,
        expected_symbol=bar.symbol,
        expected_timeframe=bar.timeframe,
    )

    assert parsed.entry_plan_id == "entry-v2"
    assert parsed.entry_price == Decimal("100")
    assert parsed.valid_until == expires_at
    assert parsed.payload["schema_version"] == "2"


def test_valid_until_conflict() -> None:
    bar = make_bar()
    feature = make_entry_feature(
        bar,
        valid_until=bar.close_time + timedelta(minutes=5),
        extra={"valid_until": (bar.close_time + timedelta(minutes=10)).isoformat()},
    )
    with pytest.raises(ProtectionTargetContractError, match="conflicts"):
        parse_entry_plan_feature(
            feature, expected_symbol=bar.symbol, expected_timeframe=bar.timeframe
        )


def test_process_key_fallback_is_bounded_and_deterministic() -> None:
    bar = make_bar()
    feature = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=bar.close_time,
        value={"entry_plan_id": "bad id"},
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        feature_id="x" * 500,
    )
    key = feature_process_key(feature)
    assert key.startswith("fingerprint:")
    assert len(key) == 76


def test_structural_selection_exact_then_generic() -> None:
    bar = make_bar()
    generic = make_structural_feature(
        bar, key="structure.target", price="104", feature_id="generic"
    )
    exact = make_structural_feature(
        bar,
        key="structure.target",
        price="105",
        feature_id="exact",
        setup_id="setup-1",
    )
    assert (
        select_structural_feature((generic, exact), setup_id="setup-1", require_setup_match=False)
        is exact
    )
    assert (
        select_structural_feature((generic,), setup_id="setup-1", require_setup_match=False)
        is generic
    )
    assert (
        select_structural_feature((generic,), setup_id="setup-1", require_setup_match=True) is None
    )


def test_structural_ambiguity_is_rejected() -> None:
    bar = make_bar()
    first = make_structural_feature(
        bar,
        key="structure.target",
        price="104",
        feature_id="a",
        setup_id="setup-1",
    )
    second = make_structural_feature(
        bar,
        key="structure.target",
        price="105",
        feature_id="b",
        setup_id="setup-1",
    )
    with pytest.raises(ProtectionTargetContractError, match="ambiguous"):
        select_structural_feature((first, second), setup_id="setup-1", require_setup_match=True)


def test_structural_equal_duplicates_choose_deterministically() -> None:
    bar = make_bar()
    first = make_structural_feature(
        bar,
        key="structure.target",
        price="104",
        feature_id="a",
        setup_id="setup-1",
    )
    second = make_structural_feature(
        bar,
        key="structure.target",
        price="104",
        feature_id="b",
        setup_id="setup-1",
    )
    # Different feature IDs are part of source identity and therefore intentionally ambiguous.
    with pytest.raises(ProtectionTargetContractError):
        select_structural_feature((first, second), setup_id="setup-1", require_setup_match=True)


def test_canonical_fingerprint_enforces_size_and_depth_bounds() -> None:
    with pytest.raises(ProtectionTargetContractError, match="string is too large"):
        canonical_fingerprint({"value": "x" * 65_537})
    with pytest.raises(ProtectionTargetContractError, match="integer is too large"):
        canonical_fingerprint({"value": 1 << 4_097})
    with pytest.raises(ProtectionTargetContractError, match="sequence is too large"):
        canonical_fingerprint([None] * 10_001)
    nested: object = "leaf"
    for _ in range(34):
        nested = [nested]
    with pytest.raises(ProtectionTargetContractError, match="nesting is too deep"):
        canonical_fingerprint(nested)


def test_safe_identifier_accepts_only_deterministic_contract_ids() -> None:
    from modular_strategy_protection_target.contracts import safe_identifier

    assert safe_identifier("entry-1") == "entry-1"
    assert safe_identifier(123) == "123"
    assert safe_identifier(True) is None
    assert safe_identifier(["entry-1"]) is None
    assert safe_identifier(object()) is None


@pytest.mark.parametrize("field", ["entry_plan_id", "setup_id"])
def test_boolean_identifiers_are_rejected(field: str) -> None:
    bar = make_bar()
    feature = make_entry_feature(bar, extra={field: True})
    with pytest.raises(ProtectionTargetContractError, match=field):
        parse_entry_plan_feature(
            feature, expected_symbol=bar.symbol, expected_timeframe=bar.timeframe
        )
