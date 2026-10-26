from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType

import pytest
from modular_core import OrderType, Side

from modular_modules.entry_model import ConfirmedSetup, EntryKind, EntryPlan, SetupPayloadError
from modular_modules.entry_model.plan import deterministic_plan_id
from modular_modules.entry_model.setup import source_feature_fingerprint

from .support import BASE_OPEN, payload, setup_feature


def valid_setup(**kwargs: object) -> ConfirmedSetup:
    defaults = {
        "setup_id": "setup-1",
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "confirmed_at": BASE_OPEN + timedelta(minutes=5),
        "confirmation_price": Decimal("100"),
        "entry_kind_hint": EntryKind.LIMIT_RETRACEMENT,
        "extreme_price": Decimal("90"),
        "metadata": {"nested": {"a": [1, Decimal("2.0")]}},
    }
    defaults.update(kwargs)
    return ConfirmedSetup(**defaults)  # type: ignore[arg-type]


def build_plan(**kwargs: object) -> EntryPlan:
    defaults = {
        "module_version": "0.2.0",
        "setup_id": "setup-1",
        "setup_fingerprint": "a" * 64,
        "config_fingerprint": "b" * 64,
        "symbol": "BTCUSDT",
        "side": Side.BUY,
        "entry_kind": EntryKind.LIMIT_RETRACEMENT,
        "order_type": OrderType.LIMIT,
        "created_at": BASE_OPEN + timedelta(minutes=5),
        "setup_confirmed_at": BASE_OPEN + timedelta(minutes=5),
        "expires_at": BASE_OPEN + timedelta(minutes=10),
        "ttl_bars": 1,
        "price": Decimal("96.18"),
        "trigger_price": None,
        "limit_price": Decimal("96.18"),
        "post_only": True,
        "rationale": "setup_hint",
        "metadata": {"x": Decimal("1.0")},
    }
    defaults.update(kwargs)
    return EntryPlan._build_without_id(**defaults)  # type: ignore[arg-type]


def test_confirmed_setup_normalizes_and_freezes_metadata() -> None:
    setup = valid_setup()
    assert setup.confirmed_at.tzinfo is UTC
    assert isinstance(setup.metadata, MappingProxyType)
    assert setup.metadata["nested"]["a"] == (1, "2")
    with pytest.raises(TypeError):
        setup.metadata["x"] = 1  # type: ignore[index]
    assert len(setup.fingerprint()) == 64
    assert setup.canonical_payload()["confirmation_price"] == "100"


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"setup_id": ""}, "setup_id"),
        ({"symbol": ""}, "symbol"),
        ({"confirmed_at": datetime(2026, 7, 21)}, "timezone-aware"),
        ({"entry_cutoff_at": datetime(2026, 7, 21)}, "timezone-aware"),
        ({"confirmation_price": Decimal("0")}, "positive finite"),
        ({"extreme_price": Decimal("NaN")}, "positive finite"),
        ({"source_schema_version": "2"}, "unsupported setup schema"),
        ({"metadata": {1: "x"}}, "keys must be strings"),
        ({"metadata": {"bad": object()}}, "unsupported value"),
    ],
)
def test_confirmed_setup_rejects_invalid(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises((ValueError, SetupPayloadError), match=match):
        valid_setup(**kwargs)


def test_from_feature_value_accepts_reference_price_and_nested_hint() -> None:
    raw = payload(
        confirmation_price=None,
        extreme_price=None,
        entry_kind=None,
        extra={"reference_price": "100"},
        entry_hint={
            "kind": "stop_limit",
            "trigger_price": "101",
            "limit_price": "101.1",
            "cutoff_at": (BASE_OPEN + timedelta(minutes=20)).isoformat(),
        },
    )
    raw.pop("confirmation_price")
    setup = ConfirmedSetup.from_feature_value(
        raw,
        feature_symbol="BTCUSDT",
        fallback_symbol="ETHUSDT",
    )
    assert setup.confirmation_price == Decimal("100")
    assert setup.entry_kind_hint is EntryKind.STOP_LIMIT
    assert setup.trigger_price == Decimal("101")
    assert setup.limit_price == Decimal("101.1")
    assert setup.entry_cutoff_at == BASE_OPEN + timedelta(minutes=20)


def test_from_feature_value_symbol_fallback_and_status_default() -> None:
    raw = payload(symbol="", status="confirmed")
    raw.pop("symbol")
    raw.pop("status")
    setup = ConfirmedSetup.from_feature_value(
        raw,
        feature_symbol="",
        fallback_symbol="BTCUSDT",
    )
    assert setup.symbol == "BTCUSDT"
    assert setup.status == "confirmed"


@pytest.mark.parametrize(
    ("value", "match"),
    [
        ([], "must be a mapping"),
        ({**payload(), "unknown": 1}, "unknown setup fields"),
        ({**payload(), "entry_hint": []}, "entry_hint must be a mapping"),
        ({**payload(), "entry_hint": {"unknown": 1}}, "unknown entry_hint fields"),
        ({**payload(), "metadata": []}, "metadata must be a mapping"),
        ({**payload(), "side": "hold"}, "side must"),
        ({**payload(), "confirmed_at": "bad"}, "ISO-8601"),
        ({**payload(), "confirmation_price": "NaN"}, "positive finite"),
        ({**payload(), "setup_id": False}, "setup_id"),
    ],
)
def test_from_feature_value_rejects(value: object, match: str) -> None:
    with pytest.raises(SetupPayloadError, match=match):
        ConfirmedSetup.from_feature_value(
            value,
            feature_symbol="BTCUSDT",
            fallback_symbol="BTCUSDT",
        )


def test_from_feature_enforces_feature_schema() -> None:
    feature = setup_feature(schema_version="2")
    with pytest.raises(SetupPayloadError, match="unsupported setup schema"):
        ConfirmedSetup.from_feature(feature, fallback_symbol="BTCUSDT")


def test_setup_fingerprint_changes_for_all_execution_inputs_and_metadata() -> None:
    base = valid_setup()
    variants = [
        replace(base, setup_id="setup-2"),
        replace(base, symbol="ETHUSDT"),
        replace(base, side=Side.SELL),
        replace(base, confirmed_at=base.confirmed_at + timedelta(seconds=1)),
        replace(base, confirmation_price=Decimal("101")),
        replace(base, status="rejected"),
        replace(base, entry_kind_hint=EntryKind.MARKET),
        replace(base, extreme_price=Decimal("89")),
        replace(base, entry_price=Decimal("95")),
        replace(base, trigger_price=Decimal("101")),
        replace(base, limit_price=Decimal("101.1")),
        replace(base, entry_cutoff_at=base.confirmed_at + timedelta(minutes=30)),
        replace(base, metadata={"different": True}),
    ]
    assert (
        len({base.fingerprint(), *(item.fingerprint() for item in variants)}) == len(variants) + 1
    )


def test_source_feature_fingerprint_is_deterministic_and_sensitive() -> None:
    first = setup_feature(metadata={"x": 1})
    same = setup_feature(metadata={"x": 1})
    changed = setup_feature(metadata={"x": 2})
    assert source_feature_fingerprint(first) == source_feature_fingerprint(same)
    assert source_feature_fingerprint(first) != source_feature_fingerprint(changed)


def test_entry_plan_roundtrip_and_immutability() -> None:
    plan = build_plan()
    value = plan.to_feature_value()
    parsed = EntryPlan.from_feature_value(value)
    assert parsed == plan
    assert parsed.plan_id.startswith("entry-")
    assert parsed.identity_payload()["price"] == "96.18"
    assert parsed.metadata["x"] == "1"
    value["price"] = "1"  # JSON-compatible export is intentionally mutable before Feature freezing.
    assert plan.price == Decimal("96.18")


def test_entry_plan_identity_detects_tampering() -> None:
    value = dict(build_plan().to_feature_value())
    changes = [
        {"price": "96.19", "limit_price": "96.19"},
        {"expires_at": (BASE_OPEN + timedelta(minutes=15)).isoformat()},
        {"post_only": False},
        {"metadata": {"x": "2"}},
        {"config_fingerprint": "c" * 64},
    ]
    for patch in changes:
        tampered = {**value, **patch}
        with pytest.raises(ValueError, match="plan_id does not match"):
            EntryPlan.from_feature_value(tampered)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"module_version": ""}, "module_version"),
        ({"setup_fingerprint": "a"}, "fingerprints"),
        ({"config_fingerprint": "b"}, "fingerprints"),
        ({"expires_at": BASE_OPEN + timedelta(minutes=5)}, "after created_at"),
        ({"ttl_bars": True}, "ttl_bars"),
        ({"ttl_bars": 0}, "ttl_bars"),
        ({"post_only": 1}, "post_only"),
        ({"price": Decimal("0")}, "positive finite"),
        ({"metadata": {"x": object()}}, "unsupported value"),
    ],
)
def test_entry_plan_rejects_invalid_common_fields(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build_plan(**kwargs)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        (
            {
                "entry_kind": EntryKind.MARKET,
                "order_type": OrderType.MARKET,
                "price": Decimal("1"),
                "limit_price": None,
                "post_only": False,
            },
            "market entry",
        ),
        (
            {
                "entry_kind": EntryKind.MARKET,
                "order_type": OrderType.MARKET,
                "price": None,
                "limit_price": None,
                "post_only": True,
            },
            "post_only",
        ),
        (
            {
                "entry_kind": EntryKind.LIMIT_EXPLICIT,
                "order_type": OrderType.LIMIT,
                "price": None,
                "limit_price": None,
            },
            "limit entry",
        ),
        (
            {
                "entry_kind": EntryKind.STOP_MARKET,
                "order_type": OrderType.STOP_MARKET,
                "price": None,
                "limit_price": None,
                "trigger_price": None,
                "post_only": False,
            },
            "stop-market",
        ),
        (
            {
                "entry_kind": EntryKind.STOP_MARKET,
                "order_type": OrderType.STOP_MARKET,
                "price": None,
                "limit_price": None,
                "trigger_price": Decimal("101"),
                "post_only": True,
            },
            "post_only",
        ),
        (
            {
                "entry_kind": EntryKind.STOP_LIMIT,
                "order_type": OrderType.STOP_LIMIT,
                "price": Decimal("101"),
                "limit_price": Decimal("101"),
                "trigger_price": None,
                "post_only": False,
            },
            "stop-limit",
        ),
        (
            {
                "entry_kind": EntryKind.STOP_LIMIT,
                "order_type": OrderType.STOP_LIMIT,
                "price": Decimal("101"),
                "limit_price": Decimal("101"),
                "trigger_price": Decimal("101"),
                "post_only": True,
            },
            "post_only",
        ),
        (
            {"entry_kind": EntryKind.MARKET, "order_type": OrderType.LIMIT},
            "entry_kind and order_type",
        ),
        (
            {
                "entry_kind": EntryKind.MARKET,
                "order_type": OrderType.TAKE_PROFIT_LIMIT,
                "price": None,
                "limit_price": None,
                "post_only": False,
            },
            "unsupported entry order type",
        ),
    ],
)
def test_entry_plan_order_field_invariants(kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        build_plan(**kwargs)


def test_entry_plan_rejects_non_mapping_unknown_schema_and_unknown_fields() -> None:
    with pytest.raises(ValueError, match="must be a mapping"):
        EntryPlan.from_feature_value([])
    with pytest.raises(ValueError, match="unsupported EntryPlan schema"):
        EntryPlan.from_feature_value({"schema_version": "3"})
    value = dict(build_plan().to_feature_value())
    value["unknown"] = 1
    with pytest.raises(ValueError, match="unknown EntryPlan fields"):
        EntryPlan.from_feature_value(value)


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("metadata", [], "metadata must be a mapping"),
        ("ttl_bars", True, "ttl_bars must be integer"),
        ("post_only", 1, "post_only must be boolean"),
        ("order_type", "bad", "invalid entry plan order_type"),
        ("side", "bad", "side must"),
        ("entry_kind", "bad", "unsupported entry kind"),
    ],
)
def test_entry_plan_parser_rejects_invalid_typed_fields(
    key: str, value: object, match: str
) -> None:
    raw = dict(build_plan().to_feature_value())
    raw[key] = value
    with pytest.raises(ValueError, match=match):
        EntryPlan.from_feature_value(raw)


def test_legacy_entry_plan_is_promoted_and_readable() -> None:
    legacy = {
        "schema_version": "1",
        "plan_id": "legacy-id",
        "setup_id": "legacy-setup",
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_kind": "limit_retracement",
        "order_type": "limit",
        "created_at": (BASE_OPEN + timedelta(minutes=5)).isoformat(),
        "setup_confirmed_at": (BASE_OPEN + timedelta(minutes=5)).isoformat(),
        "expires_at": (BASE_OPEN + timedelta(minutes=10)).isoformat(),
        "ttl_bars": 1,
        "price": "96.18",
        "trigger_price": None,
        "limit_price": "96.18",
        "post_only": True,
        "rationale": "legacy",
        "metadata": {"x": 1},
    }
    plan = EntryPlan.from_feature_value(legacy)
    assert plan.schema_version == "2"
    assert plan.module_version == "0.1.0-legacy"
    assert plan.metadata["legacy_plan_id"] == "legacy-id"
    assert EntryPlan.from_feature_value(plan.to_feature_value()) == plan


@pytest.mark.parametrize(
    ("key", "value", "match"),
    [
        ("metadata", [], "legacy entry plan metadata"),
        ("ttl_bars", True, "legacy entry plan ttl_bars"),
        ("post_only", 1, "legacy entry plan post_only"),
        ("order_type", "bad", "not a valid OrderType"),
    ],
)
def test_legacy_parser_rejects_invalid(key: str, value: object, match: str) -> None:
    legacy = {
        "setup_id": "legacy-setup",
        "symbol": "BTCUSDT",
        "side": "buy",
        "entry_kind": "market",
        "order_type": "market",
        "created_at": (BASE_OPEN + timedelta(minutes=5)).isoformat(),
        "setup_confirmed_at": (BASE_OPEN + timedelta(minutes=5)).isoformat(),
        "expires_at": (BASE_OPEN + timedelta(minutes=10)).isoformat(),
        "ttl_bars": 1,
        "price": None,
        "trigger_price": None,
        "limit_price": None,
        "post_only": False,
        "metadata": {},
    }
    legacy[key] = value
    with pytest.raises(ValueError, match=match):
        EntryPlan.from_feature_value(legacy)


def test_deterministic_plan_id_is_stable() -> None:
    identity = build_plan().identity_payload()
    assert deterministic_plan_id(identity) == deterministic_plan_id(
        dict(reversed(list(identity.items())))
    )
