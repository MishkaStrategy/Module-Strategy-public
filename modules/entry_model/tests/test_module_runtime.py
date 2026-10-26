from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from modular_core import FeatureStore

from modular_modules.entry_model import (
    EntryModelDataError,
    EntryModelModule,
    EntryPlan,
)

from .support import BASE_OPEN, bar_at, next_feature, payload, setup_feature, view_at


def plan_from(module: EntryModelModule, feature: object, *, index: int = 0) -> EntryPlan:
    decision = module.on_bar(view_at(index, features=(feature,)))  # type: ignore[arg-type]
    assert len(decision.features) == 1
    assert decision.features[0].key == "entry_model.plan"
    return EntryPlan.from_feature_value(decision.features[0].value)


def test_no_features_is_noop() -> None:
    decision = EntryModelModule().on_bar(view_at())
    assert decision.features == ()
    assert decision.skips == ()
    assert decision.diagnostics["setup_features_seen"] == 0


def test_missing_feature_view_fails_closed() -> None:
    decision = EntryModelModule().on_bar(view_at(feature_view=False))
    assert decision.features == ()
    assert decision.skips == ("entry_model.feature_view_unavailable",)


def test_data_gap_fails_closed_and_advances_cursor() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    decision = module.on_bar(view_at(features=(next_feature(0),), data_gap=True))
    assert decision.features == ()
    assert decision.skips == ("entry_model.data_gap",)
    later = module.on_bar(view_at(1, features=(next_feature(1),)))
    assert later.features[0].key == "entry_model.plan"


@pytest.mark.parametrize(
    ("side", "kind", "extra", "expected_price", "order_type"),
    [
        ("buy", "market", {}, None, "market"),
        ("sell", "market", {}, None, "market"),
        ("buy", "limit_confirmation", {}, Decimal("100"), "limit"),
        ("sell", "limit_confirmation", {}, Decimal("100"), "limit"),
        ("buy", "limit_retracement", {"extreme_price": "90"}, Decimal("96.18"), "limit"),
        ("sell", "limit_retracement", {"extreme_price": "110"}, Decimal("103.82"), "limit"),
        ("buy", "limit_explicit", {"entry_price": "99"}, Decimal("99"), "limit"),
        ("sell", "limit_explicit", {"entry_price": "101"}, Decimal("101"), "limit"),
        ("buy", "stop_market", {"trigger_price": "101"}, None, "stop_market"),
        ("sell", "stop_market", {"trigger_price": "99"}, None, "stop_market"),
        ("buy", "stop_limit", {"trigger_price": "101"}, Decimal("101"), "stop_limit"),
        ("sell", "stop_limit", {"trigger_price": "99"}, Decimal("99"), "stop_limit"),
    ],
)
def test_module_emits_all_plan_kinds(
    side: str,
    kind: str,
    extra: dict[str, str],
    expected_price: Decimal | None,
    order_type: str,
) -> None:
    payload_kwargs: dict[str, object] = {
        "side": side,
        "entry_kind": kind,
        "extreme_price": None,
        **extra,
    }
    value = payload(**payload_kwargs)  # type: ignore[arg-type]
    plan = plan_from(EntryModelModule(), setup_feature(value=value))
    assert plan.side.value == side
    assert plan.entry_kind.value == kind
    assert plan.order_type.value == order_type
    assert plan.price == expected_price


def test_setup_hint_can_be_ignored() -> None:
    feature = setup_feature(value=payload(entry_kind="market"))
    plan = plan_from(EntryModelModule(honor_setup_hint=False), feature)
    assert plan.entry_kind.value == "limit_retracement"
    assert plan.rationale == "configured_default"


def test_stop_limit_offset_is_directional() -> None:
    buy = plan_from(
        EntryModelModule(stop_limit_offset_bps="10"),
        setup_feature(
            value=payload(
                side="buy",
                entry_kind="stop_limit",
                extreme_price=None,
                trigger_price="101",
            )
        ),
    )
    sell = plan_from(
        EntryModelModule(stop_limit_offset_bps="10"),
        setup_feature(
            value=payload(
                side="sell",
                setup_id="sell",
                entry_kind="stop_limit",
                extreme_price=None,
                trigger_price="99",
            ),
            feature_id="sell-feature",
        ),
    )
    assert buy.limit_price == Decimal("101.101")
    assert sell.limit_price == Decimal("98.901")


def test_cutoff_caps_expiry() -> None:
    cutoff = BASE_OPEN + timedelta(minutes=20)
    value = payload(entry_cutoff_at=cutoff)
    plan = plan_from(EntryModelModule(ttl_bars=100), setup_feature(value=value))
    assert plan.expires_at == cutoff


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ({"bad": True}, "entry_model.invalid_setup_payload"),
        (payload(status="armed"), "entry_model.setup_not_confirmed"),
        (payload(symbol="ETHUSDT"), "entry_model.symbol_mismatch"),
        (
            payload(confirmed_at=BASE_OPEN + timedelta(minutes=5, microseconds=-1)),
            "entry_model.stale_setup",
        ),
        (
            payload(confirmed_at=BASE_OPEN + timedelta(minutes=5, seconds=1)),
            "entry_model.feature_causality_violation",
        ),
        (
            payload(entry_kind="limit_explicit", extreme_price=None),
            "entry_model.required_price_missing",
        ),
        (
            payload(entry_kind="limit_explicit", extreme_price=None, entry_price="101"),
            "entry_model.invalid_price_geometry",
        ),
        (
            payload(entry_cutoff_at=BASE_OPEN + timedelta(minutes=5)),
            "entry_model.expired_at_creation",
        ),
    ],
)
def test_rejections_are_structured(value: dict[str, object], reason: str) -> None:
    decision = EntryModelModule().on_bar(view_at(features=(setup_feature(value=value),)))
    assert reason in decision.skips
    assert decision.features[0].key == "entry_model.rejection"
    assert decision.features[0].value["reason"] == reason


def test_unsupported_setup_schema() -> None:
    decision = EntryModelModule().on_bar(view_at(features=(setup_feature(schema_version="9"),)))
    assert "entry_model.unsupported_setup_schema" in decision.skips


def test_disallowed_hint_is_rejected() -> None:
    module = EntryModelModule(allowed_entry_kinds=("limit_retracement",))
    decision = module.on_bar(view_at(features=(setup_feature(value=payload(entry_kind="market")),)))
    assert "entry_model.entry_kind_not_allowed" in decision.skips


def test_exact_duplicate_bar_is_ignored() -> None:
    module = EntryModelModule()
    module.on_bar(view_at())
    decision = module.on_bar(view_at())
    assert decision.skips == ("entry_model.duplicate_event",)


def test_conflicting_duplicate_bar_raises() -> None:
    module = EntryModelModule()
    module.on_bar(view_at())
    with pytest.raises(EntryModelDataError, match="conflicting duplicate"):
        module.on_bar(view_at(bar=bar_at(close=99.5)))


def test_out_of_order_bar_raises() -> None:
    module = EntryModelModule()
    module.on_bar(view_at(1))
    with pytest.raises(EntryModelDataError, match="out-of-order"):
        module.on_bar(view_at(0))


@pytest.mark.parametrize(
    "changed_bar",
    [bar_at(1, symbol="ETHUSDT"), bar_at(1, timeframe="15m")],
)
def test_stream_switch_raises(changed_bar: object) -> None:
    module = EntryModelModule()
    module.on_bar(view_at())
    with pytest.raises(EntryModelDataError, match="cannot switch"):
        module.on_bar(view_at(bar=changed_bar))  # type: ignore[arg-type]


def test_duplicate_setup_next_bar_is_idempotent() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    module.on_bar(view_at(features=(next_feature(0),)))
    duplicate_value = payload(setup_id="setup-0", confirmed_at=bar_at(0).close_time)
    decision = module.on_bar(
        view_at(
            1,
            features=(
                setup_feature(
                    value=duplicate_value,
                    index=1,
                    feature_id="new-source",
                ),
            ),
        )
    )
    assert decision.features == ()
    assert "entry_model.duplicate_setup" in decision.skips


def test_source_feature_conflict_across_bars() -> None:
    module = EntryModelModule(max_setup_age_seconds=600)
    module.on_bar(view_at(features=(next_feature(0, feature_id="same-source"),)))
    changed = next_feature(1, setup_id="different", feature_id="same-source")
    decision = module.on_bar(view_at(1, features=(changed,)))
    assert "entry_model.source_feature_conflict" in decision.skips


@pytest.mark.parametrize("reverse", [False, True])
def test_conflicting_setup_revisions_are_order_independent(reverse: bool) -> None:
    first = payload(setup_id="same")
    second = deepcopy(first)
    second["extreme_price"] = "80"
    features = [
        setup_feature(value=first, feature_id="a"),
        setup_feature(value=second, feature_id="b"),
    ]
    if reverse:
        features.reverse()
    decision = EntryModelModule().on_bar(view_at(features=features))
    assert all(feature.key == "entry_model.rejection" for feature in decision.features)
    assert "entry_model.setup_revision_conflict" in decision.skips


def test_same_feature_id_conflict_within_batch() -> None:
    first = setup_feature(value=payload(setup_id="a"), feature_id="same")
    second = setup_feature(value=payload(setup_id="b"), feature_id="same")
    decision = EntryModelModule().on_bar(view_at(features=(first, second)))
    assert "entry_model.source_feature_conflict" in decision.skips


def test_exact_duplicate_batch_processes_once() -> None:
    value = payload()
    decision = EntryModelModule().on_bar(
        view_at(
            features=(
                setup_feature(value=deepcopy(value), feature_id="a"),
                setup_feature(value=deepcopy(value), feature_id="b"),
            )
        )
    )
    assert sum(feature.key == "entry_model.plan" for feature in decision.features) == 1
    assert decision.diagnostics["duplicates_ignored"] == 1


def test_anonymous_malformed_duplicates_do_not_collide() -> None:
    bad = payload(extra={"unsupported": True})
    features = (
        setup_feature(value=deepcopy(bad), feature_id=None),
        setup_feature(value=deepcopy(bad), feature_id=None),
    )
    decision = EntryModelModule().on_bar(view_at(features=features))
    store = FeatureStore()
    for feature in decision.features:
        store.publish(feature)
    assert len(store.all()) == 1


@pytest.mark.parametrize(
    "metadata",
    [
        {"bad": {1, 2}},
        {"bad": object()},
    ],
)
def test_unsupported_metadata_becomes_rejection_not_crash(metadata: object) -> None:
    decision = EntryModelModule().on_bar(
        view_at(features=(setup_feature(value=payload(metadata=metadata)),))
    )
    assert "entry_model.invalid_setup_payload" in decision.skips


def test_rejection_ids_are_distinct() -> None:
    malformed = setup_feature(value={"bad": True}, feature_id="bad")
    armed = setup_feature(value=payload(setup_id="armed", status="armed"), feature_id="armed")
    decision = EntryModelModule().on_bar(view_at(features=(malformed, armed)))
    ids = [feature.feature_id for feature in decision.features]
    assert len(ids) == len(set(ids))
