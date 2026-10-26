from __future__ import annotations

from datetime import timedelta

import pytest

from modular_setup_adapter import (
    REJECTION_FEATURE_KEY,
    SETUP_FEATURE_KEY,
    AdapterReason,
    CanonicalSetupAdapterModule,
    SetupAdapterDataError,
)

from .conftest import bar, breakout_feature, view


def test_sell_breakout_maps_to_canonical_setup() -> None:
    current = bar()
    module = CanonicalSetupAdapterModule()
    decision = module.on_bar(view(current, (breakout_feature(current),)))
    assert decision.skips == ()
    assert len(decision.features) == 1
    feature = decision.features[0]
    assert feature.key == SETUP_FEATURE_KEY
    assert feature.source_module == module.name
    assert feature.created_at == current.close_time
    assert feature.valid_until == current.close_time
    assert feature.schema_version == "1"
    assert feature.value["side"] == "sell"
    assert feature.value["confirmation_price"] == "100.9"
    assert feature.value["extreme_price"] == "103"
    assert feature.value["confirmed_at"] == current.close_time.isoformat()
    assert feature.value["metadata"]["source"]["bars_to_return"] == 1


def test_buy_breakout_maps_with_inverse_geometry() -> None:
    current = bar(close=99.1)
    feature = breakout_feature(
        current,
        side="buy",
        return_close="99.1",
        extreme="97",
        payload_overrides={"setup_id": "buy-1"},
    )
    decision = CanonicalSetupAdapterModule().on_bar(view(current, (feature,)))
    assert decision.features[0].value["side"] == "buy"
    assert decision.features[0].value["extreme_price"] == "97"


@pytest.mark.parametrize(
    "kwargs,reason",
    [
        ({"schema_version": "9"}, AdapterReason.UNSUPPORTED_SOURCE_SCHEMA),
        ({"payload_overrides": {"symbol": "ETHUSDT"}}, AdapterReason.SYMBOL_MISMATCH),
        ({"return_close": "bad"}, AdapterReason.INVALID_SOURCE_PAYLOAD),
        ({"side": "sell", "extreme": "99"}, AdapterReason.INVALID_PRICE_GEOMETRY),
        (
            {"payload_overrides": {"timeframe": "15m"}},
            AdapterReason.TIMEFRAME_MISMATCH,
        ),
    ],
)
def test_invalid_source_emits_typed_rejection(
    kwargs: dict[str, object], reason: AdapterReason
) -> None:
    current = bar()
    feature = breakout_feature(current, **kwargs)
    decision = CanonicalSetupAdapterModule().on_bar(view(current, (feature,)))
    assert decision.skips == (reason.value,)
    assert decision.features[0].key == REJECTION_FEATURE_KEY
    assert decision.features[0].value["reason"] == reason.value


def test_future_or_stale_publication_rejected_as_causality_violation() -> None:
    current = bar()
    earlier = current.close_time - timedelta(minutes=5)
    feature = breakout_feature(
        current,
        created_at=current.close_time,
        payload_overrides={"confirmation_time": earlier.isoformat()},
    )
    decision = CanonicalSetupAdapterModule().on_bar(view(current, (feature,)))
    assert decision.features[0].value["reason"] == AdapterReason.FEATURE_CAUSALITY_VIOLATION.value


def test_repeated_source_is_idempotently_skipped() -> None:
    current = bar()
    feature = breakout_feature(current)
    module = CanonicalSetupAdapterModule()
    first = module.on_bar(view(current, (feature,)))
    assert first.features[0].key == SETUP_FEATURE_KEY
    duplicate_bar = module.on_bar(view(current, (feature,)))
    assert duplicate_bar.features == ()
    assert duplicate_bar.skips == (AdapterReason.DUPLICATE_EVENT.value,)


def test_reused_source_id_with_changed_content_is_rejected() -> None:
    first_bar = bar()
    second_bar = bar(1)
    module = CanonicalSetupAdapterModule()
    first = breakout_feature(first_bar, feature_id="same-id")
    module.on_bar(view(first_bar, (first,)))
    conflicting = breakout_feature(
        second_bar,
        feature_id="same-id",
        return_close="100.8",
        extreme="103.1",
        payload_overrides={"confirmation_time": second_bar.close_time.isoformat()},
    )
    decision = module.on_bar(view(second_bar, (conflicting,)))
    assert decision.features[0].key == REJECTION_FEATURE_KEY
    assert decision.features[0].value["reason"] == AdapterReason.SOURCE_FEATURE_CONFLICT.value


def test_out_of_order_and_changed_duplicate_bars_raise() -> None:
    module = CanonicalSetupAdapterModule()
    first = bar(1)
    module.on_bar(view(first))
    with pytest.raises(SetupAdapterDataError, match="out-of-order"):
        module.on_bar(view(bar(0)))

    other = CanonicalSetupAdapterModule()
    current = bar()
    other.on_bar(view(current))
    changed = bar(close=100.8)
    with pytest.raises(SetupAdapterDataError, match="contradictory duplicate"):
        other.on_bar(view(changed))


def test_data_gap_missing_feature_view_and_direct_guard() -> None:
    current = bar()
    gap_view = view(current)
    object.__setattr__(gap_view, "data_gap", True)
    module = CanonicalSetupAdapterModule()
    assert module.on_bar(gap_view).skips == (AdapterReason.DATA_GAP.value,)

    no_features = view(bar(1))
    object.__setattr__(no_features, "features", None)
    second = CanonicalSetupAdapterModule()
    assert second.on_bar(no_features).skips == (AdapterReason.FEATURE_VIEW_UNAVAILABLE.value,)
    with pytest.raises(RuntimeError, match="feature view"):
        second._process_sources(no_features)  # type: ignore[attr-defined]


def test_source_module_and_feature_timeframe_mismatch_rejected() -> None:
    current = bar()
    configured = CanonicalSetupAdapterModule(
        sources=[
            {
                "feature_key": "breakout_return.confirmed",
                "profile": "breakout_return_v1",
                "source_module": "expected_source",
            }
        ]
    )
    source_mismatch = configured.on_bar(view(current, (breakout_feature(current),)))
    assert source_mismatch.features[0].key == REJECTION_FEATURE_KEY

    current2 = bar(1)
    wrong_tf = breakout_feature(current2)
    object.__setattr__(wrong_tf, "timeframe", "15m")
    decision = CanonicalSetupAdapterModule().on_bar(view(current2, (wrong_tf,)))
    assert decision.features[0].value["reason"] == AdapterReason.TIMEFRAME_MISMATCH.value


def test_invalid_signal_direction_and_mapping_payload_rejected() -> None:
    current = bar()
    invalid_side = breakout_feature(current, side="hold")
    decision = CanonicalSetupAdapterModule().on_bar(view(current, (invalid_side,)))
    assert decision.features[0].value["reason"] == AdapterReason.INVALID_SOURCE_PAYLOAD.value

    current2 = bar(1)
    contradiction = breakout_feature(
        current2,
        side="sell",
        payload_overrides={
            "breakout_direction": "below",
            "confirmation_time": current2.close_time.isoformat(),
        },
    )
    decision2 = CanonicalSetupAdapterModule().on_bar(view(current2, (contradiction,)))
    assert decision2.features[0].value["reason"] == AdapterReason.INVALID_PRICE_GEOMETRY.value

    current3 = bar(2)
    non_mapping = breakout_feature(current3)
    object.__setattr__(non_mapping, "value", "invalid")
    decision3 = CanonicalSetupAdapterModule().on_bar(view(current3, (non_mapping,)))
    assert decision3.features[0].value["reason"] == AdapterReason.INVALID_SOURCE_PAYLOAD.value


def test_batch_duplicate_and_conflict_paths() -> None:
    current = bar()
    first = breakout_feature(current, feature_id="same")
    duplicate = breakout_feature(current, feature_id="same")
    decision = CanonicalSetupAdapterModule().on_bar(view(current, (first, duplicate)))
    assert len([item for item in decision.features if item.key == SETUP_FEATURE_KEY]) == 1
    assert AdapterReason.DUPLICATE_SOURCE.value in decision.skips

    current2 = bar(1)
    first2 = breakout_feature(
        current2,
        feature_id="conflict",
        payload_overrides={"confirmation_time": current2.close_time.isoformat()},
    )
    changed2 = breakout_feature(
        current2,
        feature_id="conflict",
        extreme="104",
        payload_overrides={"confirmation_time": current2.close_time.isoformat()},
    )
    conflict = CanonicalSetupAdapterModule().on_bar(view(current2, (first2, changed2)))
    assert AdapterReason.SOURCE_FEATURE_CONFLICT.value in conflict.skips
    assert any(item.key == REJECTION_FEATURE_KEY for item in conflict.features)


def test_fingerprint_failure_emits_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    current = bar()

    def fail(_feature: object) -> str:
        raise ValueError("cannot normalize")

    monkeypatch.setattr("modular_setup_adapter.module.source_feature_fingerprint", fail)
    decision = CanonicalSetupAdapterModule().on_bar(
        view(current, (breakout_feature(current, feature_id="bad-normalization"),))
    )
    assert decision.features[0].value["reason"] == AdapterReason.INVALID_SOURCE_PAYLOAD.value


def test_stream_switch_is_rejected() -> None:
    module = CanonicalSetupAdapterModule()
    module.on_bar(view(bar()))
    switched = bar(1)
    object.__setattr__(switched, "symbol", "ETHUSDT")
    with pytest.raises(SetupAdapterDataError, match="cannot switch"):
        module.on_bar(view(switched))
