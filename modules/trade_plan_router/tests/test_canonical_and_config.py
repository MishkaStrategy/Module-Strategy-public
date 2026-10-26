from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router.canonical import canonical_json, sha256_hex, stable_id
from modular_strategy_trade_plan_router.config import RouterConfiguration
from modular_strategy_trade_plan_router.errors import RouterConfigurationError


def test_decimal_scale_does_not_change_hash():
    assert sha256_hex({"value": Decimal("1.0")}) == sha256_hex({"value": Decimal("1.000")})


def test_mapping_order_does_not_change_hash():
    assert sha256_hex({"a": 1, "b": 2}) == sha256_hex({"b": 2, "a": 1})


def test_timezone_equivalent_datetimes_are_canonical():
    utc = datetime(2026, 7, 23, 2, tzinfo=UTC)
    plus_two = datetime.fromisoformat("2026-07-23T04:00:00+02:00")
    assert canonical_json(utc) == canonical_json(plus_two)


def test_non_finite_decimal_rejected():
    with pytest.raises(ValueError, match="non-finite"):
        canonical_json(Decimal("NaN"))


def test_stable_id_is_reproducible():
    assert stable_id("test", {"x": 1}) == stable_id("test", {"x": 1})


def test_unknown_config_field_rejected():
    with pytest.raises(RouterConfigurationError, match="unknown"):
        RouterConfiguration.from_mapping({"surprise": True})


def test_config_hash_changes_with_policy():
    first = RouterConfiguration().fingerprint
    second = RouterConfiguration(plan_ttl_seconds=301).fingerprint
    assert first != second


def test_priority_maps_are_order_independent():
    first = RouterConfiguration(setup_priorities={"a": 1, "b": 2})
    second = RouterConfiguration(setup_priorities={"b": 2, "a": 1})
    assert first.fingerprint == second.fingerprint


def test_resource_limits_validate_and_affect_configuration_hash():
    with pytest.raises(RouterConfigurationError, match="max_entry_candidates"):
        RouterConfiguration(max_entry_candidates=0)
    with pytest.raises(RouterConfigurationError, match="max_pending_orders"):
        RouterConfiguration.from_mapping({"max_pending_orders": True})

    baseline = RouterConfiguration()
    constrained = RouterConfiguration.from_mapping(
        {
            "max_entry_candidates": 5,
            "max_decisions_per_type": 7,
            "max_portfolio_positions": 11,
            "max_pending_orders": 13,
            "max_targets_per_plan": 17,
        }
    )
    assert constrained.max_entry_candidates == 5
    assert constrained.max_decisions_per_type == 7
    assert constrained.max_portfolio_positions == 11
    assert constrained.max_pending_orders == 13
    assert constrained.max_targets_per_plan == 17
    assert constrained.fingerprint != baseline.fingerprint
    assert constrained.semantic_fingerprint != baseline.semantic_fingerprint


def test_str_enum_is_canonicalized_to_plain_string():
    from modular_strategy_trade_plan_router.enums import ReasonCode
    from modular_strategy_trade_plan_router.canonical import canonical_value

    value = canonical_value({"reason": ReasonCode.INVALID_INPUT})
    assert value == {"reason": "invalid_input"}
    assert type(value["reason"]) is str


def test_priority_map_keys_are_nfc_normalized_trimmed_and_log_safe():
    decomposed = RouterConfiguration(setup_priorities={"  cafe\u0301  ": 7})
    composed = RouterConfiguration(setup_priorities={"caf\u00e9": 7})
    assert decomposed.setup_priorities == {"caf\u00e9": 7}
    assert decomposed.fingerprint == composed.fingerprint

    with pytest.raises(RouterConfigurationError, match="control characters"):
        RouterConfiguration(setup_priorities={"setup\nforged": 1})
    with pytest.raises(RouterConfigurationError, match="maximum length"):
        RouterConfiguration(setup_priorities={"x" * 513: 1})


def test_priority_map_rejects_duplicate_keys_after_normalization():
    with pytest.raises(RouterConfigurationError, match="duplicate normalized key"):
        RouterConfiguration(setup_priorities={"cafe\u0301": 1, "caf\u00e9": 2})


def test_documented_config_example_loads_strictly():
    from pathlib import Path

    import yaml

    example = Path(__file__).resolve().parents[1] / "config.example.yaml"
    payload = yaml.safe_load(example.read_text(encoding="utf-8"))
    config = RouterConfiguration.from_mapping(payload)
    assert config.require_portfolio_snapshot is True
    assert config.side_priorities == {"long": 0, "short": 0}
    assert config.audit_level == "full"


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("strict_mode", "false", "strict_mode must be boolean"),
        ("require_portfolio_snapshot", 1, "require_portfolio_snapshot must be boolean"),
        (
            "allow_risk_reducing_when_locked",
            None,
            "allow_risk_reducing_when_locked must be boolean",
        ),
        ("warning_escalation", "true", "warning_escalation must be boolean"),
    ],
)
def test_direct_configuration_rejects_non_boolean_flags(field, value, message):
    with pytest.raises(RouterConfigurationError, match=message):
        RouterConfiguration(**{field: value})


def test_direct_configuration_rejects_untyped_policy():
    with pytest.raises(RouterConfigurationError, match="long_short_policy"):
        RouterConfiguration(long_short_policy="higher_priority")

    with pytest.raises(RouterConfigurationError, match="active_plan_policy"):
        RouterConfiguration(active_plan_policy="allow_multiple")


def test_active_plan_policy_mapping_is_strict_and_semantic():
    from modular_strategy_trade_plan_router import ActivePlanPolicy

    default = RouterConfiguration()
    allowed = RouterConfiguration.from_mapping({"active_plan_policy": "allow_multiple"})

    assert default.active_plan_policy is ActivePlanPolicy.REJECT_NEW_EXPOSURE
    assert allowed.active_plan_policy is ActivePlanPolicy.ALLOW_MULTIPLE
    assert default.semantic_fingerprint != allowed.semantic_fingerprint

    with pytest.raises(RouterConfigurationError, match="active_plan_policy"):
        RouterConfiguration.from_mapping({"active_plan_policy": True})


def test_direct_configuration_normalizes_decimal_limits():
    config = RouterConfiguration(
        max_portfolio_risk_fraction="0.025",
        max_symbol_notional_fraction=2,
    )
    assert config.max_portfolio_risk_fraction == Decimal("0.025")
    assert config.max_symbol_notional_fraction == Decimal("2")
    assert isinstance(config.max_portfolio_risk_fraction, Decimal)
    assert isinstance(config.max_symbol_notional_fraction, Decimal)


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1},
        {"audit_level": 1},
    ],
)
def test_mapping_configuration_rejects_coerced_text_fields(payload):
    with pytest.raises(RouterConfigurationError):
        RouterConfiguration.from_mapping(payload)


class _DuplicateConfigurationMapping(Mapping[str, object]):
    def __iter__(self):
        return iter(("strict_mode", "strict_mode"))

    def __len__(self) -> int:
        return 2

    def __getitem__(self, key: str) -> object:
        if key == "strict_mode":
            return True
        raise KeyError(key)

    def items(self):
        return iter((("strict_mode", True), ("strict_mode", False)))


class _HostileConfigurationKey:
    def __str__(self) -> str:
        raise AssertionError("configuration boundary must not stringify hostile keys")


def test_configuration_mapping_snapshot_rejects_non_string_and_duplicate_keys():
    with pytest.raises(RouterConfigurationError, match="exact non-blank strings"):
        RouterConfiguration.from_mapping({_HostileConfigurationKey(): True})  # type: ignore[dict-item]

    with pytest.raises(RouterConfigurationError, match="duplicate key 'strict_mode'"):
        RouterConfiguration.from_mapping(_DuplicateConfigurationMapping())


def test_priority_ranks_are_bounded_before_fingerprinting():
    for rank in (2_147_483_648, -2_147_483_648):
        with pytest.raises(RouterConfigurationError, match="signed 32-bit range"):
            RouterConfiguration(strategy_priorities={"strategy": rank})

    edge = RouterConfiguration(
        strategy_priorities={
            "minimum": -2_147_483_647,
            "maximum": 2_147_483_647,
        }
    )
    assert len(edge.fingerprint) == 64


def test_decimal_canonicalization_is_independent_of_global_context():
    from decimal import ROUND_DOWN, ROUND_HALF_EVEN, localcontext

    values = (
        Decimal("123456789.123456789123456789"),
        Decimal("0.000000000123456789123456789"),
        Decimal("-987654321000000000000.0000000001000"),
    )
    outputs = []
    for precision, rounding in (
        (6, ROUND_DOWN),
        (10, ROUND_HALF_EVEN),
        (28, ROUND_HALF_EVEN),
        (80, ROUND_DOWN),
    ):
        with localcontext() as context:
            context.prec = precision
            context.rounding = rounding
            outputs.append(tuple(canonical_json(value) for value in values))

    assert len(set(outputs)) == 1
    assert outputs[0] == (
        '"123456789.123456789123456789"',
        '"0.000000000123456789123456789"',
        '"-987654321000000000000.0000000001"',
    )


def test_decimal_canonicalization_rejects_pathological_exponents_without_allocating():
    with pytest.raises(ValueError, match="maximum text length"):
        canonical_json(Decimal("1e1000001"))
    with pytest.raises(ValueError, match="maximum text length"):
        canonical_json(Decimal("1e-1000001"))


def test_side_priority_keys_are_strict_and_effective():
    with pytest.raises(RouterConfigurationError, match="unsupported keys"):
        RouterConfiguration(side_priorities={"LONG": 7})
    with pytest.raises(RouterConfigurationError, match="unsupported keys"):
        RouterConfiguration.from_mapping({"side_priorities": {"buy": 7}})

    config = RouterConfiguration(side_priorities={"long": 7, "short": -2})
    from modular_strategy_trade_plan_router import TradeSide

    assert (
        config.priority_for(
            portfolio_id="portfolio",
            strategy_id="strategy",
            setup_id="setup",
            module_id="module",
            side=TradeSide.LONG,
        )[-1]
        == 7
    )


@pytest.mark.parametrize(
    "field",
    [
        "max_decision_age_seconds",
        "max_snapshot_age_seconds",
        "plan_ttl_seconds",
        "max_entry_candidates",
        "max_decisions_per_type",
        "max_portfolio_positions",
        "max_pending_orders",
        "max_targets_per_plan",
    ],
)
def test_configuration_positive_integer_fields_are_bounded(field):
    with pytest.raises(RouterConfigurationError, match="range"):
        RouterConfiguration(**{field: 2_147_483_648})
    with pytest.raises(RouterConfigurationError, match="range"):
        RouterConfiguration.from_mapping({field: 2_147_483_648})


@pytest.mark.parametrize(
    "field",
    [
        "max_entry_candidates",
        "max_decisions_per_type",
        "max_portfolio_positions",
        "max_pending_orders",
        "max_targets_per_plan",
    ],
)
def test_collection_configuration_fields_respect_global_hard_ceiling(field: str) -> None:
    from modular_strategy_trade_plan_router.limits import MAX_ROUTER_COLLECTION_ITEMS

    with pytest.raises(RouterConfigurationError, match="must be in range"):
        RouterConfiguration(**{field: MAX_ROUTER_COLLECTION_ITEMS + 1})
    with pytest.raises(RouterConfigurationError, match="must be in range"):
        RouterConfiguration.from_mapping({field: MAX_ROUTER_COLLECTION_ITEMS + 1})


def test_control_character_fast_path_preserves_unicode_category_policy() -> None:
    from modular_strategy_trade_plan_router.canonical import has_control_characters

    assert has_control_characters("") is False
    assert has_control_characters("plain-ascii") is False
    assert has_control_characters("line\nbreak") is True
    assert has_control_characters("zero\u200bwidth") is True
    assert has_control_characters("private\ue000use") is True
    assert has_control_characters("non\u00a0breaking") is True
    assert has_control_characters("line\u2028separator") is True
    assert has_control_characters("visible-é-😀") is False
