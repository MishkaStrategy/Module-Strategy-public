from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from modular_strategy_trade_plan_router import DecisionNormalizer, TradeSide
from modular_strategy_trade_plan_router.canonical import sha256_hex
from modular_strategy_trade_plan_router.errors import RouterInputError

from conftest import NOW


def actual_entry_payload() -> dict[str, object]:
    identity: dict[str, object] = {
        "schema_version": "2",
        "module_version": "0.2.0",
        "setup_id": "setup-a",
        "setup_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_kind": "limit_explicit",
        "order_type": "limit",
        "created_at": NOW.isoformat(),
        "setup_confirmed_at": NOW.isoformat(),
        "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
        "ttl_bars": 12,
        "price": "100",
        "trigger_price": None,
        "limit_price": "100",
        "post_only": False,
        "rationale": "fixture",
        "metadata": {"strategy_id": "strategy-a", "market": "usdt_perpetual"},
    }
    return {"plan_id": "entry-" + sha256_hex(identity)[:32], **identity}


def legacy_entry_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "entry_plan_id": "entry-legacy",
        "setup_id": "setup-a",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "side": "buy",
        "entry_price": "100",
        "created_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
    }


def protection_payload(entry_id: str | None = None) -> dict[str, object]:
    return {
        "schema_version": "1",
        "protection_plan_id": "protection-a",
        "entry_plan_id": entry_id or str(actual_entry_payload()["plan_id"]),
        "setup_id": "setup-a",
        "symbol": "BTCUSDT",
        "timeframe": "5m",
        "side": "buy",
        "entry_price": "100",
        "invalidation_price": "94",
        "stop_loss": "95",
        "take_profit": "110",
        "risk_distance": "5",
        "reward_distance": "10",
        "stop_distance_bps": "500",
        "reward_r": "2",
        "invalidation_mode": "explicit",
        "target_mode": "r_multiple",
        "created_at": NOW.isoformat(),
        "valid_until": (NOW + timedelta(minutes=5)).isoformat(),
    }


def sizing_payload() -> dict[str, object]:
    return {
        "schema_version": "1",
        "request_id": "risk-a",
        "symbol": "BTCUSDT",
        "status": "approved",
        "approved": True,
        "quantity": "10",
        "raw_quantity": "10.1",
        "risk_fraction": "0.005",
        "risk_budget": "50",
        "actual_risk_amount": "50",
        "actual_risk_fraction": "0.005",
        "notional": "1000",
        "required_leverage": "1",
        "binding_constraints": [],
        "reject_reason": None,
        "message": "approved",
        "setup_id": "setup-a",
        "diagnostics": {
            "entry_plan_id": str(actual_entry_payload()["plan_id"]),
            "protection_plan_id": "protection-a",
        },
    }


def test_actual_entry_schema_2_is_normalized():
    value = DecisionNormalizer().normalize_entry(
        actual_entry_payload(),
        feature_key="entry_model.plan",
        feature_schema_version="2",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="feature-entry",
        source_module="entry_model",
        symbol="BTCUSDT",
        portfolio_id="portfolio-a",
    )
    assert value.candidate_id.startswith("entry-")
    assert value.side is TradeSide.LONG
    assert value.limit_price == Decimal("100")


def test_legacy_entry_schema_1_is_normalized():
    value = DecisionNormalizer().normalize_entry(
        legacy_entry_payload(),
        feature_key="entry_model.entry_plan",
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="feature-entry",
        source_module="entry_model",
        symbol="BTCUSDT",
        portfolio_id="portfolio-a",
    )
    assert value.candidate_id == "entry-legacy"
    assert value.order_type == "limit"


def test_actual_entry_key_does_not_silently_accept_unknown_schema():
    payload = actual_entry_payload()
    payload["schema_version"] = "99"
    with pytest.raises(RouterInputError, match="unsupported entry plan schema"):
        DecisionNormalizer().normalize_entry(
            payload,
            feature_key="entry_model.plan",
            feature_schema_version="99",
            feature_created_at=NOW,
            feature_valid_until=None,
            feature_id="feature-entry",
            source_module="entry_model",
            symbol="BTCUSDT",
        )


def test_tampered_entry_identity_is_rejected():
    payload = actual_entry_payload()
    payload["symbol"] = "ETHUSDT"
    with pytest.raises(RouterInputError, match="plan_id does not match"):
        DecisionNormalizer().normalize_entry(
            payload,
            feature_key="entry_model.plan",
            feature_schema_version="2",
            feature_created_at=NOW,
            feature_valid_until=None,
            feature_id="feature-entry",
            source_module="entry_model",
            symbol="BTCUSDT",
        )


def test_entry_symbol_conflict_is_rejected_after_valid_identity():
    payload = actual_entry_payload()
    payload["symbol"] = "ETHUSDT"
    identity = {key: value for key, value in payload.items() if key != "plan_id"}
    payload["plan_id"] = "entry-" + sha256_hex(identity)[:32]
    with pytest.raises(RouterInputError, match="symbol conflicts"):
        DecisionNormalizer().normalize_entry(
            payload,
            feature_key="entry_model.plan",
            feature_schema_version="2",
            feature_created_at=NOW,
            feature_valid_until=None,
            feature_id="feature-entry",
            source_module="entry_model",
            symbol="BTCUSDT",
        )


def test_protection_schema_is_normalized():
    value = DecisionNormalizer().normalize_protection(
        protection_payload(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="feature-protection",
        symbol="BTCUSDT",
    )
    assert value.entry_candidate_id == actual_entry_payload()["plan_id"]
    assert value.stop_loss == Decimal("95")
    assert value.targets[0].price == Decimal("110")


def test_sizing_schema_is_normalized():
    value = DecisionNormalizer().normalize_sizing(
        sizing_payload(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="feature-sizing",
        symbol="BTCUSDT",
    )
    assert value.approved
    assert value.quantity == Decimal("10")
    assert value.entry_candidate_id == actual_entry_payload()["plan_id"]


def test_same_bar_sizing_evidence_is_a_valid_router_input() -> None:
    value = DecisionNormalizer().normalize_sizing(
        sizing_payload(),
        feature_schema_version="1",
        feature_created_at=NOW,
        feature_valid_until=NOW,
        feature_id="feature-sizing-same-bar",
        symbol="BTCUSDT",
    )

    assert value.created_at == NOW
    assert value.expires_at == NOW
    assert value.approved is True


def test_sizing_evidence_cannot_expire_before_creation() -> None:
    with pytest.raises(RouterInputError, match="cannot be before created_at"):
        DecisionNormalizer().normalize_sizing(
            sizing_payload(),
            feature_schema_version="1",
            feature_created_at=NOW,
            feature_valid_until=NOW - timedelta(microseconds=1),
            feature_id="feature-sizing-invalid-expiry",
            symbol="BTCUSDT",
        )


def test_sizing_status_and_approved_flag_must_agree():
    payload = sizing_payload()
    payload["approved"] = False
    with pytest.raises(RouterInputError, match="conflicts"):
        DecisionNormalizer().normalize_sizing(
            payload,
            feature_schema_version="1",
            feature_created_at=NOW,
            feature_valid_until=None,
            feature_id="feature-sizing",
            symbol="BTCUSDT",
        )


def test_upstream_contract_key_mismatch_is_explicitly_visible():
    assert "entry_model.plan" in DecisionNormalizer.ENTRY_KEYS
    assert "entry_model.entry_plan" in DecisionNormalizer.ENTRY_KEYS
    assert DecisionNormalizer.ENTRY_KEYS[0] != DecisionNormalizer.ENTRY_KEYS[1]


def test_strict_mode_rejects_unknown_entry_field():
    payload = actual_entry_payload()
    payload["unexpected"] = "value"
    with pytest.raises(RouterInputError, match="unknown EntryPlan"):
        DecisionNormalizer(strict_mode=True).normalize_entry(
            payload,
            feature_key="entry_model.plan",
            feature_schema_version="2",
            feature_created_at=NOW,
            feature_valid_until=NOW + timedelta(minutes=5),
            feature_id="feature-entry",
            source_module="entry_model",
            symbol="BTCUSDT",
        )


def test_lenient_mode_ignores_unknown_entry_field_without_changing_identity():
    payload = actual_entry_payload()
    payload["unexpected"] = "value"
    value = DecisionNormalizer(strict_mode=False).normalize_entry(
        payload,
        feature_key="entry_model.plan",
        feature_schema_version="2",
        feature_created_at=NOW,
        feature_valid_until=NOW + timedelta(minutes=5),
        feature_id="feature-entry",
        source_module="entry_model",
        symbol="BTCUSDT",
        portfolio_id="portfolio-a",
    )
    assert value.candidate_id == actual_entry_payload()["plan_id"]
