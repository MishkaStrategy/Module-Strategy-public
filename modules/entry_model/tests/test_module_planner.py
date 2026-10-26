from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import MappingProxyType, SimpleNamespace

import pytest
from modular_core import Feature, OrderType, Side

from modular_modules.entry_model import (
    ENTRY_PLAN_FEATURE_KEY,
    ENTRY_REJECTION_FEATURE_KEY,
    EntryKind,
    EntryModelDataError,
    EntryModelModule,
    EntryPlan,
    EntryRejectionReason,
)
from modular_modules.entry_model.planner import (
    build_entry_plan,
    validate_limit_geometry,
    validate_retracement_geometry,
    validate_stop_geometry,
    validate_stop_limit_geometry,
)
from modular_modules.entry_model.setup import ConfirmedSetup

from .support import bar_at, next_feature, payload, setup_feature, view_at


def assert_plan(decision: object) -> EntryPlan:
    features = decision.features  # type: ignore[attr-defined]
    assert len(features) == 1
    feature = features[0]
    assert feature.key == ENTRY_PLAN_FEATURE_KEY
    assert feature.schema_version == "2"
    assert feature.invalidate_on_gap is True
    return EntryPlan.from_feature_value(feature.value)


def assert_rejection(decision: object, reason: EntryRejectionReason) -> Feature:
    features = decision.features  # type: ignore[attr-defined]
    assert len(features) == 1
    feature = features[0]
    assert feature.key == ENTRY_REJECTION_FEATURE_KEY
    assert feature.value["reason"] == reason.value
    assert feature.schema_version == "2"
    assert feature.invalidate_on_gap is False
    assert reason.value in decision.skips  # type: ignore[attr-defined]
    return feature


@pytest.mark.parametrize(
    ("side", "expected"),
    [("buy", Decimal("96.18")), ("sell", Decimal("103.82"))],
)
def test_limit_retracement_buy_sell(side: str, expected: Decimal) -> None:
    feature = next_feature(0, side=side)
    decision = EntryModelModule(ttl_bars=2).on_bar(view_at(0, features=(feature,)))
    plan = assert_plan(decision)
    assert plan.price == expected
    assert plan.limit_price == expected
    assert plan.trigger_price is None
    assert plan.order_type is OrderType.LIMIT
    assert plan.post_only is True
    assert plan.expires_at == bar_at(0).close_time + timedelta(minutes=10)
    assert plan.rationale == "setup_hint"


@pytest.mark.parametrize(
    ("kind", "side", "kwargs", "expected"),
    [
        ("market", "buy", {"extreme_price": None}, (None, None, None, OrderType.MARKET, False)),
        (
            "limit_confirmation",
            "buy",
            {"extreme_price": None},
            (Decimal("100"), None, Decimal("100"), OrderType.LIMIT, True),
        ),
        (
            "limit_explicit",
            "buy",
            {"extreme_price": None, "entry_price": "99"},
            (Decimal("99"), None, Decimal("99"), OrderType.LIMIT, True),
        ),
        (
            "limit_explicit",
            "sell",
            {"extreme_price": None, "entry_price": "101"},
            (Decimal("101"), None, Decimal("101"), OrderType.LIMIT, True),
        ),
        (
            "stop_market",
            "buy",
            {"extreme_price": None, "trigger_price": "101"},
            (None, Decimal("101"), None, OrderType.STOP_MARKET, False),
        ),
        (
            "stop_market",
            "sell",
            {"extreme_price": None, "trigger_price": "99"},
            (None, Decimal("99"), None, OrderType.STOP_MARKET, False),
        ),
        (
            "stop_limit",
            "buy",
            {"extreme_price": None, "trigger_price": "101", "limit_price": "101.5"},
            (Decimal("101.5"), Decimal("101"), Decimal("101.5"), OrderType.STOP_LIMIT, False),
        ),
        (
            "stop_limit",
            "sell",
            {"extreme_price": None, "trigger_price": "99", "limit_price": "98.5"},
            (Decimal("98.5"), Decimal("99"), Decimal("98.5"), OrderType.STOP_LIMIT, False),
        ),
    ],
)
def test_all_entry_models(
    kind: str,
    side: str,
    kwargs: dict[str, object],
    expected: tuple[Decimal | None, Decimal | None, Decimal | None, OrderType, bool],
) -> None:
    feature = next_feature(0, side=side, entry_kind=kind, **kwargs)
    plan = assert_plan(EntryModelModule().on_bar(view_at(0, features=(feature,))))
    assert (
        plan.price,
        plan.trigger_price,
        plan.limit_price,
        plan.order_type,
        plan.post_only,
    ) == expected


@pytest.mark.parametrize(
    ("side", "expected_limit"),
    [("buy", Decimal("101.101")), ("sell", Decimal("98.901"))],
)
def test_stop_limit_offset_fallback(side: str, expected_limit: Decimal) -> None:
    trigger = "101" if side == "buy" else "99"
    feature = next_feature(
        0,
        side=side,
        entry_kind="stop_limit",
        extreme_price=None,
        trigger_price=trigger,
    )
    module = EntryModelModule(stop_limit_offset_bps="10")
    plan = assert_plan(module.on_bar(view_at(0, features=(feature,))))
    assert plan.limit_price == expected_limit


def test_stop_models_allow_entry_price_as_trigger_fallback() -> None:
    feature = next_feature(
        0,
        entry_kind="stop_market",
        extreme_price=None,
        entry_price="101",
    )
    plan = assert_plan(EntryModelModule().on_bar(view_at(0, features=(feature,))))
    assert plan.trigger_price == Decimal("101")


def test_module_can_ignore_setup_hint() -> None:
    feature = next_feature(0, entry_kind="market", extreme_price=None)
    module = EntryModelModule(honor_setup_hint=False, default_entry_kind="limit_confirmation")
    plan = assert_plan(module.on_bar(view_at(0, features=(feature,))))
    assert plan.entry_kind is EntryKind.LIMIT_CONFIRMATION
    assert plan.rationale == "configured_default"


def test_post_only_can_be_disabled() -> None:
    feature = next_feature(0, entry_kind="limit_confirmation", extreme_price=None)
    plan = assert_plan(
        EntryModelModule(post_only_limits=False).on_bar(view_at(0, features=(feature,)))
    )
    assert plan.post_only is False


def test_cutoff_caps_expiry() -> None:
    cutoff = bar_at(0).close_time + timedelta(minutes=7)
    feature = next_feature(0, entry_cutoff_at=cutoff)
    plan = assert_plan(EntryModelModule(ttl_bars=10).on_bar(view_at(0, features=(feature,))))
    assert plan.expires_at == cutoff


def test_upstream_ttl_can_shorten_but_not_extend_configured_ceiling() -> None:
    short = next_feature(0, metadata={"entry_ttl_bars": 3})
    short_plan = assert_plan(EntryModelModule(ttl_bars=10).on_bar(view_at(0, features=(short,))))
    assert short_plan.ttl_bars == 3
    assert short_plan.expires_at == bar_at(0).close_time + timedelta(minutes=15)

    long = next_feature(0, metadata={"entry_ttl_bars": 20})
    long_plan = assert_plan(EntryModelModule(ttl_bars=10).on_bar(view_at(0, features=(long,))))
    assert long_plan.ttl_bars == 10
    assert long_plan.expires_at == bar_at(0).close_time + timedelta(minutes=50)


@pytest.mark.parametrize("value", [True, 0, -1, "3"])
def test_invalid_upstream_ttl_metadata_rejects(value: object) -> None:
    feature = next_feature(0, metadata={"entry_ttl_bars": value})
    assert_rejection(
        EntryModelModule(ttl_bars=10).on_bar(view_at(0, features=(feature,))),
        EntryRejectionReason.INVALID_SETUP_PAYLOAD,
    )


def test_router_metadata_is_promoted_without_losing_source_metadata() -> None:
    source_metadata = {
        "priority": -2,
        "historical_priority": 2,
        "strategy_id": "fvg_rt",
        "module_id": "frozen_1h_short",
        "market": "binance_usdtm",
        "confidence": "0.75",
        "incompatibility_tags": ["single-position"],
        "exclusivity_group": "fvg_rt",
        "correlation_group": "btc",
        "reason_codes": ["canonical_fvg"],
        "target_price": "89",
    }
    feature = next_feature(0, metadata=source_metadata)

    plan = assert_plan(EntryModelModule().on_bar(view_at(0, features=(feature,))))

    assert plan.metadata["priority"] == -2
    assert plan.metadata["strategy_id"] == "fvg_rt"
    assert plan.metadata["module_id"] == "frozen_1h_short"
    assert plan.metadata["market"] == "binance_usdtm"
    assert plan.metadata["confidence"] == "0.75"
    assert plan.metadata["incompatibility_tags"] == ("single-position",)
    assert plan.metadata["exclusivity_group"] == "fvg_rt"
    assert plan.metadata["correlation_group"] == "btc"
    assert plan.metadata["reason_codes"] == ("canonical_fvg",)
    source = plan.metadata["source_setup_metadata"]
    assert isinstance(source, Mapping)
    assert source["historical_priority"] == 2
    assert source["target_price"] == "89"
    assert "historical_priority" not in plan.metadata
    assert "target_price" not in plan.metadata


@pytest.mark.parametrize(
    ("side", "kind", "kwargs", "reason"),
    [
        (
            "buy",
            "limit_retracement",
            {"extreme_price": None},
            EntryRejectionReason.REQUIRED_PRICE_MISSING,
        ),
        (
            "buy",
            "limit_retracement",
            {"extreme_price": "100"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "sell",
            "limit_retracement",
            {"extreme_price": "100"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "buy",
            "limit_explicit",
            {"extreme_price": None},
            EntryRejectionReason.REQUIRED_PRICE_MISSING,
        ),
        (
            "buy",
            "limit_explicit",
            {"extreme_price": None, "entry_price": "101"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "sell",
            "limit_explicit",
            {"extreme_price": None, "entry_price": "99"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "buy",
            "stop_market",
            {"extreme_price": None},
            EntryRejectionReason.REQUIRED_PRICE_MISSING,
        ),
        (
            "buy",
            "stop_market",
            {"extreme_price": None, "trigger_price": "99"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "sell",
            "stop_market",
            {"extreme_price": None, "trigger_price": "101"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "buy",
            "stop_limit",
            {"extreme_price": None, "trigger_price": "101", "limit_price": "100"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
        (
            "sell",
            "stop_limit",
            {"extreme_price": None, "trigger_price": "99", "limit_price": "100"},
            EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        ),
    ],
)
def test_required_prices_and_invalid_geometry_reject(
    side: str,
    kind: str,
    kwargs: dict[str, object],
    reason: EntryRejectionReason,
) -> None:
    feature = next_feature(0, side=side, entry_kind=kind, **kwargs)
    assert_rejection(EntryModelModule().on_bar(view_at(0, features=(feature,))), reason)


def test_expired_at_creation_rejects() -> None:
    cutoff = bar_at(0).close_time
    feature = next_feature(0, entry_cutoff_at=cutoff)
    assert_rejection(
        EntryModelModule().on_bar(view_at(0, features=(feature,))),
        EntryRejectionReason.EXPIRED_AT_CREATION,
    )


def test_invalid_timeframe_rejects() -> None:
    bar = bar_at(0, timeframe="base")
    feature = setup_feature(
        value=payload(confirmed_at=bar.close_time),
        created_at=bar.close_time,
        timeframe="base",
        valid_until=bar.close_time,
    )
    assert_rejection(
        EntryModelModule().on_bar(view_at(0, bar=bar, features=(feature,), timeframe="base")),
        EntryRejectionReason.INVALID_TIMEFRAME,
    )


def test_setup_status_symbol_causality_stale_and_disabled_kind() -> None:
    cases: list[tuple[EntryModelModule, Feature, EntryRejectionReason]] = []
    cases.append(
        (
            EntryModelModule(),
            next_feature(0, status="armed"),
            EntryRejectionReason.SETUP_NOT_CONFIRMED,
        )
    )
    cases.append(
        (
            EntryModelModule(),
            next_feature(0, symbol="ETHUSDT"),
            EntryRejectionReason.SYMBOL_MISMATCH,
        )
    )
    bar = bar_at(0)
    causality = setup_feature(
        value=payload(confirmed_at=bar.close_time + timedelta(seconds=1)),
        created_at=bar.close_time,
        valid_until=bar.close_time,
    )
    cases.append((EntryModelModule(), causality, EntryRejectionReason.FEATURE_CAUSALITY_VIOLATION))
    old = setup_feature(
        value=payload(confirmed_at=bar.close_time - timedelta(seconds=1)),
        created_at=bar.close_time,
        valid_until=bar.close_time,
    )
    cases.append((EntryModelModule(), old, EntryRejectionReason.STALE_SETUP))
    cases.append(
        (
            EntryModelModule(allowed_entry_kinds=("market",), default_entry_kind="market"),
            next_feature(0),
            EntryRejectionReason.ENTRY_KIND_NOT_ALLOWED,
        )
    )
    for module, feature, reason in cases:
        assert_rejection(module.on_bar(view_at(0, features=(feature,))), reason)


def test_future_setup_validation_direct_branch() -> None:
    module = EntryModelModule(max_setup_age_seconds=100)
    bar = bar_at(0)
    feature = setup_feature(created_at=bar.close_time, valid_until=bar.close_time)
    setup = ConfirmedSetup.from_feature_value(
        payload(confirmed_at=bar.close_time + timedelta(seconds=1)),
        feature_symbol="BTCUSDT",
        fallback_symbol="BTCUSDT",
    )
    # Causality normally wins; a synthetic feature published equally in the future exercises FUTURE_SETUP.
    future_feature = Feature(
        key=feature.key,
        source_module=feature.source_module,
        created_at=bar.close_time + timedelta(seconds=1),
        value=feature.value,
        symbol=feature.symbol,
        timeframe=feature.timeframe,
        valid_until=bar.close_time + timedelta(seconds=1),
        feature_id=feature.feature_id,
    )
    result = module._validate_setup(future_feature, setup, view_at(0))
    assert result is not None and result[0] is EntryRejectionReason.FUTURE_SETUP


def test_missing_feature_view_gap_and_no_features() -> None:
    missing = EntryModelModule().on_bar(view_at(0, feature_view=False))
    assert missing.skips == (EntryRejectionReason.FEATURE_VIEW_UNAVAILABLE.value,)
    gap = EntryModelModule().on_bar(view_at(0, data_gap=True))
    assert gap.skips == (EntryRejectionReason.DATA_GAP.value,)
    empty = EntryModelModule().on_bar(view_at(0))
    assert empty.features == ()
    assert empty.diagnostics["setup_features_seen"] == 0


def test_exact_duplicate_bar_conflicting_bar_out_of_order_and_stream_switch() -> None:
    module = EntryModelModule()
    first = view_at(1)
    module.on_bar(first)
    duplicate = module.on_bar(first)
    assert duplicate.skips == (EntryRejectionReason.DUPLICATE_EVENT.value,)
    conflicting = bar_at(1, close=100.5, high=101.0)
    with pytest.raises(EntryModelDataError, match="conflicting duplicate bar"):
        module.on_bar(view_at(1, bar=conflicting))
    with pytest.raises(EntryModelDataError, match="out-of-order"):
        module.on_bar(view_at(0))
    with pytest.raises(EntryModelDataError, match="cannot switch symbol"):
        module.on_bar(view_at(2, symbol="ETHUSDT"))


def test_switch_timeframe_rejected() -> None:
    module = EntryModelModule()
    module.on_bar(view_at(0))
    with pytest.raises(EntryModelDataError, match="cannot switch symbol or timeframe"):
        module.on_bar(view_at(1, timeframe="15m", bar=bar_at(1, timeframe="15m")))


def test_duplicate_setup_across_bars_is_idempotent() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    first = next_feature(0, setup_id="same", feature_id="f1")
    assert_plan(module.on_bar(view_at(0, features=(first,))))
    second = setup_feature(
        value=payload(setup_id="same", confirmed_at=bar_at(0).close_time),
        index=1,
        feature_id="f2",
        created_at=bar_at(1).close_time,
        valid_until=bar_at(1).close_time,
    )
    decision = module.on_bar(view_at(1, features=(second,)))
    assert decision.features == ()
    assert EntryRejectionReason.DUPLICATE_SETUP.value in decision.skips


def test_setup_revision_conflict_across_bars() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    first = next_feature(0, setup_id="same", feature_id="f1")
    assert_plan(module.on_bar(view_at(0, features=(first,))))
    changed = setup_feature(
        value=payload(setup_id="same", confirmed_at=bar_at(0).close_time, extreme_price="89"),
        index=1,
        feature_id="f2",
        created_at=bar_at(1).close_time,
        valid_until=bar_at(1).close_time,
    )
    assert_rejection(
        module.on_bar(view_at(1, features=(changed,))),
        EntryRejectionReason.SETUP_REVISION_CONFLICT,
    )


def test_batch_setup_conflict_is_order_independent() -> None:
    one = next_feature(0, setup_id="same", feature_id="a")
    two = next_feature(0, setup_id="same", feature_id="b", extreme_price="89")
    decisions = []
    for features in ((one, two), (two, one)):
        decision = EntryModelModule().on_bar(view_at(0, features=features))
        rejection = assert_rejection(decision, EntryRejectionReason.SETUP_REVISION_CONFLICT)
        decisions.append(rejection.feature_id)
        assert not any(feature.key == ENTRY_PLAN_FEATURE_KEY for feature in decision.features)
    assert decisions[0] == decisions[1]


def test_batch_exact_duplicate_setup_different_source_ids_creates_one_plan() -> None:
    one = next_feature(0, setup_id="same", feature_id="a")
    two = next_feature(0, setup_id="same", feature_id="b")
    decision = EntryModelModule().on_bar(view_at(0, features=(two, one)))
    assert len([f for f in decision.features if f.key == ENTRY_PLAN_FEATURE_KEY]) == 1
    assert decision.diagnostics["duplicates_ignored"] == 1


def test_same_source_id_duplicate_and_conflict_in_batch() -> None:
    same1 = next_feature(0, setup_id="a", feature_id="source")
    same2 = next_feature(0, setup_id="a", feature_id="source")
    duplicate = EntryModelModule().on_bar(view_at(0, features=(same1, same2)))
    assert len([f for f in duplicate.features if f.key == ENTRY_PLAN_FEATURE_KEY]) == 1
    assert duplicate.diagnostics["duplicates_ignored"] == 1

    changed = next_feature(0, setup_id="b", feature_id="source", extreme_price="89")
    conflict = EntryModelModule().on_bar(view_at(0, features=(same1, changed)))
    assert any(
        feature.value["reason"] == EntryRejectionReason.SOURCE_FEATURE_CONFLICT.value
        for feature in conflict.features
    )


def test_source_id_reuse_conflict_across_bars() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    first = next_feature(0, setup_id="a", feature_id="source")
    assert_plan(module.on_bar(view_at(0, features=(first,))))
    changed = setup_feature(
        value=payload(setup_id="b", confirmed_at=bar_at(1).close_time),
        index=1,
        feature_id="source",
    )
    assert_rejection(
        module.on_bar(view_at(1, features=(changed,))),
        EntryRejectionReason.SOURCE_FEATURE_CONFLICT,
    )


def test_malformed_payload_and_unsupported_schema_rejections_are_deterministic() -> None:
    malformed = setup_feature(value={"setup_id": "x"}, feature_id=None)
    decision = EntryModelModule().on_bar(view_at(0, features=(malformed,)))
    first = assert_rejection(decision, EntryRejectionReason.INVALID_SETUP_PAYLOAD)
    replay = EntryModelModule().on_bar(view_at(0, features=(malformed,)))
    second = assert_rejection(replay, EntryRejectionReason.INVALID_SETUP_PAYLOAD)
    assert first.feature_id == second.feature_id

    unsupported = setup_feature(schema_version="2", feature_id="v2")
    assert_rejection(
        EntryModelModule().on_bar(view_at(0, features=(unsupported,))),
        EntryRejectionReason.UNSUPPORTED_SETUP_SCHEMA,
    )


def test_malformed_fingerprint_value_fails_closed() -> None:
    feature = setup_feature(value={"bad": object()}, feature_id="bad-object")
    assert_rejection(
        EntryModelModule().on_bar(view_at(0, features=(feature,))),
        EntryRejectionReason.INVALID_SETUP_PAYLOAD,
    )


def test_two_identical_malformed_inputs_without_id_do_not_collide_in_feature_store() -> None:
    one = setup_feature(value={"bad": object()}, feature_id=None)
    two = setup_feature(value={"bad": object()}, feature_id=None)
    decision = EntryModelModule().on_bar(view_at(0, features=(one, two)))
    assert len(decision.features) == 1
    assert decision.diagnostics["duplicates_ignored"] == 1


def test_gap_does_not_consume_setup_and_can_retry_next_bar_when_age_allowed() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    feature = next_feature(0, setup_id="gap")
    gap_decision = module.on_bar(view_at(0, features=(feature,), data_gap=True))
    assert gap_decision.features == ()
    retry_feature = setup_feature(
        value=payload(setup_id="gap", confirmed_at=bar_at(0).close_time),
        index=1,
        feature_id="gap-retry",
    )
    assert_plan(module.on_bar(view_at(1, features=(retry_feature,))))


def test_multiple_unique_setups_on_one_bar_are_sorted_by_setup_id() -> None:
    b = next_feature(0, setup_id="b", feature_id="fb")
    a = next_feature(0, setup_id="a", feature_id="fa")
    decision = EntryModelModule().on_bar(view_at(0, features=(b, a)))
    plans = [EntryPlan.from_feature_value(f.value) for f in decision.features]
    assert [plan.setup_id for plan in plans] == ["a", "b"]


def test_rejection_output_and_diagnostics_are_immutable() -> None:
    feature = next_feature(0, extreme_price=None)
    decision = EntryModelModule().on_bar(view_at(0, features=(feature,)))
    rejection = assert_rejection(decision, EntryRejectionReason.REQUIRED_PRICE_MISSING)
    assert isinstance(decision.diagnostics, MappingProxyType)
    assert isinstance(decision.diagnostics["rejection_counts"], MappingProxyType)
    assert rejection.value["config_fingerprint"]
    with pytest.raises(TypeError):
        decision.diagnostics["x"] = 1  # type: ignore[index]


def test_reason_mapping_branches() -> None:
    mapping = {
        "required price missing: x": EntryRejectionReason.REQUIRED_PRICE_MISSING,
        "invalid price geometry: x": EntryRejectionReason.INVALID_PRICE_GEOMETRY,
        "entry plan expired at creation": EntryRejectionReason.EXPIRED_AT_CREATION,
        "entry kind not allowed: x": EntryRejectionReason.ENTRY_KIND_NOT_ALLOWED,
        "current timeframe has no positive duration": EntryRejectionReason.INVALID_TIMEFRAME,
        "entry expiry overflow": EntryRejectionReason.INVALID_TIMEFRAME,
        "unexpected": EntryRejectionReason.INTERNAL_ERROR,
    }
    for message, reason in mapping.items():
        assert EntryModelModule._reason_from_build_error(message) is reason


def test_retracement_geometry_missing_extreme_fails_without_assert_dependency() -> None:
    setup = ConfirmedSetup(
        "missing-extreme",
        "BTCUSDT",
        Side.BUY,
        bar_at(0).close_time,
        Decimal("100"),
        extreme_price=None,
    )
    with pytest.raises(ValueError, match="required price missing: extreme_price"):
        validate_retracement_geometry(setup)


def test_geometry_helper_boundaries() -> None:
    buy = ConfirmedSetup(
        "b", "BTCUSDT", Side.BUY, bar_at(0).close_time, Decimal("100"), extreme_price=Decimal("90")
    )
    sell = ConfirmedSetup(
        "s",
        "BTCUSDT",
        Side.SELL,
        bar_at(0).close_time,
        Decimal("100"),
        extreme_price=Decimal("110"),
    )
    validate_retracement_geometry(buy)
    validate_retracement_geometry(sell)
    validate_limit_geometry(Side.BUY, Decimal("100"), Decimal("100"))
    validate_limit_geometry(Side.SELL, Decimal("100"), Decimal("100"))
    validate_stop_geometry(Side.BUY, Decimal("100"), Decimal("100"))
    validate_stop_geometry(Side.SELL, Decimal("100"), Decimal("100"))
    validate_stop_limit_geometry(Side.BUY, Decimal("100"), Decimal("100"))
    validate_stop_limit_geometry(Side.SELL, Decimal("100"), Decimal("100"))


def test_planner_direct_build_and_expiry_overflow() -> None:
    module = EntryModelModule()
    setup = ConfirmedSetup(
        setup_id="x",
        symbol="BTCUSDT",
        side=Side.BUY,
        confirmed_at=datetime.max.replace(tzinfo=UTC) - timedelta(days=1),
        confirmation_price=Decimal("100"),
        entry_kind_hint=EntryKind.MARKET,
    )
    current = SimpleNamespace(
        close_time=datetime.max.replace(tzinfo=UTC) - timedelta(seconds=1),
        open_time=datetime.max.replace(tzinfo=UTC) - timedelta(days=1),
    )
    fake_view = SimpleNamespace(current=current)
    with pytest.raises(ValueError, match="entry expiry overflow"):
        build_entry_plan(
            config=module.config,
            manifest_version="0.2.0",
            setup=setup,
            setup_fingerprint=setup.fingerprint(),
            view=fake_view,  # type: ignore[arg-type]
        )
