from __future__ import annotations

from modular_core import FeatureStore, MarketView

from conftest import bullish_triplet, view_for
from modular_strategy_fvg import FvgModule


def _view_with_store(view: MarketView, store: FeatureStore) -> MarketView:
    return MarketView(
        base_bars=view.base_bars,
        timeframe_bars=view.timeframe_bars,
        index=view.index,
        cash=view.cash,
        equity=view.equity,
        position=view.position,
        pending_orders=view.pending_orders,
        exit_pending=view.exit_pending,
        data_gap=view.data_gap,
        features=store.snapshot(view.current.close_time),
        readiness=view.readiness,
        sessions=view.sessions,
    )


def _active_feature_id(decision: object) -> str:
    features = getattr(decision, "features")
    active = [feature for feature in features if feature.key == "fvg.active_zone"]
    assert len(active) == 1
    feature_id = active[0].feature_id
    assert feature_id is not None
    return feature_id


def test_module_only_restore_still_republishes_active_zone_once() -> None:
    bars = bullish_triplet()
    original = FvgModule(timeframes=("15m",))
    created = original.on_bar(view_for(bars))
    active_id = _active_feature_id(created)

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(original.snapshot_state())

    first = restored.on_bar(view_for(bars))
    assert first.diagnostics["republished_after_restore"] == 1
    assert any(feature.feature_id == active_id for feature in first.features)

    second = restored.on_bar(view_for(bars))
    assert not any(feature.feature_id == active_id for feature in second.features)


def test_restore_reuses_full_feature_store_without_duplicate_republish() -> None:
    bars = bullish_triplet()
    view = view_for(bars)
    original = FvgModule(timeframes=("15m",))
    created = original.on_bar(view)
    active_id = _active_feature_id(created)

    store = FeatureStore()
    for feature in created.features:
        store.publish(feature)
    assert any(feature.feature_id == active_id for feature in store.all())

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(original.snapshot_state())

    decision = restored.on_bar(_view_with_store(view, store))
    assert decision.diagnostics["restore_republish_suppressed"] == 1
    assert decision.diagnostics["pre_restore_republish_suppressed"] == 0
    assert not any(feature.feature_id == active_id for feature in decision.features)

    for feature in decision.features:
        store.publish(feature)


def test_unrelated_visible_fvg_features_do_not_suppress_module_restore_republish() -> None:
    bars = bullish_triplet()
    view = view_for(bars)
    original = FvgModule(timeframes=("15m",))
    created = original.on_bar(view)
    active_id = _active_feature_id(created)

    store = FeatureStore()
    for feature in created.features:
        if feature.key == "fvg.zone_created":
            store.publish(feature)

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(original.snapshot_state())

    decision = restored.on_bar(_view_with_store(view, store))
    assert decision.diagnostics["republished_after_restore"] == 1
    assert any(feature.feature_id == active_id for feature in decision.features)
