from __future__ import annotations

from datetime import timedelta
from types import MappingProxyType

import pytest
from conftest import BASE_TIME, make_bar, make_entry_feature, make_view
from modular_core import Candle, Feature, FeatureView, MarketView, ModuleDecision, StrategyModule

from modular_strategy_protection_target import (
    ProtectionTargetDataError,
    ProtectionTargetModule,
)


def test_public_module_contract(view: MarketView) -> None:
    module = ProtectionTargetModule()
    assert isinstance(module, StrategyModule)
    decision = module.on_bar(view)
    assert isinstance(decision, ModuleDecision)
    assert len(decision.features) == 1
    assert not decision.orders
    assert not decision.signals
    assert not decision.setups
    assert not decision.cancellations
    assert not decision.position_updates
    assert not decision.closes
    feature = decision.features[0]
    assert feature.key == "protection_target.plan"
    assert feature.source_module == "protection_target"
    assert feature.schema_version == "1"
    assert feature.invalidate_on_gap is True
    assert isinstance(feature.value, MappingProxyType)


def test_empty_and_missing_inputs(bar: Candle) -> None:
    empty = MarketView(
        base_bars=(),
        timeframe_bars={},
        index=0,
        cash=0,
        equity=0,
        position=None,
        pending_orders=(),
        features=FeatureView((), BASE_TIME),
    )
    assert ProtectionTargetModule().on_bar(empty).skips == ("protection_target.empty_market_view",)
    assert ProtectionTargetModule().on_bar(make_view(bar, include_feature_view=False)).skips == (
        "protection_target.no_feature_view",
    )
    assert ProtectionTargetModule().on_bar(make_view(bar)).skips == (
        "protection_target.no_entry_plan",
    )


def test_stale_entry_behavior(bar: Candle) -> None:
    old = make_bar(-1)
    stale = make_entry_feature(old, valid_until=bar.close_time)
    strict = ProtectionTargetModule().on_bar(make_view(bar, stale))
    assert strict.skips == ("protection_target.stale_entry_plan",)
    relaxed = ProtectionTargetModule(require_same_bar_entry=False).on_bar(make_view(bar, stale))
    assert relaxed.features[0].key == "protection_target.plan"


@pytest.mark.parametrize("offset", [-5, 5])
def test_feature_snapshot_must_equal_current_close(bar: Candle, offset: int) -> None:
    entry = make_entry_feature(bar)
    view = make_view(
        bar,
        entry,
        feature_timestamp=bar.close_time + timedelta(minutes=offset),
    )
    with pytest.raises(ProtectionTargetDataError, match="must equal"):
        ProtectionTargetModule().on_bar(view)


def test_data_gap_fails_closed_and_does_not_consume(bar: Candle) -> None:
    module = ProtectionTargetModule()
    entry = make_entry_feature(bar)
    decision = module.on_bar(make_view(bar, entry, data_gap=True))
    assert decision.features == ()
    assert decision.skips == ("protection_target.data_gap_skipped",)
    next_bar = make_bar(1)
    next_entry = make_entry_feature(next_bar, plan_id="next", setup_id="next")
    accepted = module.on_bar(make_view(next_bar, next_entry))
    assert accepted.features[0].value["entry_plan_id"] == "next"


def test_exact_duplicate_is_idempotent(view: MarketView) -> None:
    module = ProtectionTargetModule()
    assert module.on_bar(view).features
    duplicate = module.on_bar(view)
    assert duplicate.features == ()
    assert duplicate.skips == ("protection_target.duplicate_event_ignored",)
    assert module.metrics["duplicate_bars"] == 1


def test_changed_feature_context_on_same_bar_is_conflicting(bar: Candle) -> None:
    module = ProtectionTargetModule()
    first = make_entry_feature(bar, feature_id="first")
    module.on_bar(make_view(bar, first))
    changed = make_entry_feature(
        bar,
        feature_id="second",
        entry_price="100.1",
        invalidation_price="99",
    )
    with pytest.raises(ProtectionTargetDataError, match="duplicate event"):
        module.on_bar(make_view(bar, changed))


def test_changed_gap_context_on_same_bar_is_conflicting(bar: Candle) -> None:
    module = ProtectionTargetModule()
    entry = make_entry_feature(bar)
    module.on_bar(make_view(bar, entry, data_gap=True))
    with pytest.raises(ProtectionTargetDataError, match="duplicate event"):
        module.on_bar(make_view(bar, entry, data_gap=False))


def test_conflicting_bar_and_ordering(bar: Candle) -> None:
    module = ProtectionTargetModule()
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    conflicting = Candle(
        timestamp=bar.timestamp,
        open=100,
        high=103,
        low=98,
        close=101,
        volume=10,
        symbol=bar.symbol,
        timeframe=bar.timeframe,
    )
    with pytest.raises(ProtectionTargetDataError, match="duplicate bar"):
        module.on_bar(make_view(conflicting, make_entry_feature(conflicting, plan_id="c")))
    old = make_bar(-1)
    with pytest.raises(ProtectionTargetDataError, match="out-of-order"):
        module.on_bar(make_view(old, make_entry_feature(old, plan_id="old")))


def test_stream_switch_rejected(bar: Candle) -> None:
    module = ProtectionTargetModule()
    module.on_bar(make_view(bar, make_entry_feature(bar)))
    eth = make_bar(1, symbol="ETHUSDT")
    with pytest.raises(ProtectionTargetDataError, match="cannot switch"):
        module.on_bar(make_view(eth, make_entry_feature(eth, plan_id="eth")))


def test_batch_order_is_deterministic(bar: Candle) -> None:
    buy = make_entry_feature(bar, plan_id="a", setup_id="a", feature_id="a")
    sell = make_entry_feature(
        bar,
        plan_id="b",
        setup_id="b",
        side="sell",
        invalidation_price="101",
        feature_id="b",
    )
    forward = ProtectionTargetModule().on_bar(make_view(bar, buy, sell))
    reverse = ProtectionTargetModule().on_bar(make_view(bar, sell, buy))
    assert [f.value["protection_plan_id"] for f in forward.features] == [
        f.value["protection_plan_id"] for f in reverse.features
    ]


def test_processed_plan_not_republished_later(bar: Candle) -> None:
    module = ProtectionTargetModule(require_same_bar_entry=False)
    entry = make_entry_feature(bar, valid_until=bar.close_time + timedelta(minutes=10))
    module.on_bar(make_view(bar, entry))
    next_bar = make_bar(1)
    duplicate = module.on_bar(make_view(next_bar, entry))
    assert duplicate.features == ()
    assert duplicate.skips == ("protection_target.entry_plan_already_processed",)


def test_reused_plan_id_conflict_is_atomic(bar: Candle) -> None:
    module = ProtectionTargetModule(require_same_bar_entry=False)
    original = make_entry_feature(bar, valid_until=bar.close_time + timedelta(minutes=20))
    module.on_bar(make_view(bar, original))
    before = module.snapshot_state()
    next_bar = make_bar(1)
    fresh = make_entry_feature(
        next_bar,
        plan_id="fresh",
        setup_id="fresh",
        valid_until=next_bar.close_time,
        feature_id="a-fresh",
    )
    conflict = make_entry_feature(
        bar,
        entry_price="101",
        invalidation_price="99",
        valid_until=next_bar.close_time,
        feature_id="z-conflict",
    )
    with pytest.raises(ProtectionTargetDataError, match="conflicting content"):
        module.on_bar(make_view(next_bar, fresh, conflict))
    assert module.snapshot_state() == before


def test_malformed_entry_rejection_and_disable(bar: Candle) -> None:
    malformed = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=bar.close_time,
        value={"entry_plan_id": "entry-1", "setup_id": "setup-1"},
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        valid_until=bar.close_time,
        feature_id="malformed",
    )
    decision = ProtectionTargetModule().on_bar(make_view(bar, malformed))
    assert decision.features[0].key == "protection_target.rejected"
    assert decision.features[0].value["reason"] == ("protection_target.invalid_entry_plan")
    disabled = ProtectionTargetModule(emit_rejection_feature=False).on_bar(
        make_view(bar, malformed)
    )
    assert disabled.features == ()
    assert disabled.skips == ("protection_target.invalid_entry_plan",)


def test_diagnostic_results_are_bounded(bar: Candle) -> None:
    features = tuple(
        make_entry_feature(
            bar,
            plan_id=f"entry-{index}",
            setup_id=f"setup-{index}",
            feature_id=f"feature-{index}",
        )
        for index in range(20)
    )
    decision = ProtectionTargetModule(max_diagnostic_results=3).on_bar(make_view(bar, *features))
    assert len(decision.diagnostics["results"]) == 3
    assert decision.diagnostics["results_truncated"] == 17


def test_metrics_are_immutable(view: MarketView) -> None:
    module = ProtectionTargetModule()
    module.on_bar(view)
    assert module.metrics["accepted_plans"] == 1
    with pytest.raises(TypeError):
        module.metrics["accepted_plans"] = 2


def test_lifecycle_hooks_are_safe(view: MarketView) -> None:
    module = ProtectionTargetModule()
    module.on_start()
    module.on_bar(view)
    module.on_finish()


def test_gap_event_ignores_unusable_entry_payload_for_safety(bar: Candle) -> None:
    malformed = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=bar.close_time,
        value=object(),
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        valid_until=bar.close_time,
        feature_id="malformed-gap-payload",
    )
    decision = ProtectionTargetModule().on_bar(make_view(bar, malformed, data_gap=True))
    assert decision.features == ()
    assert decision.skips == ("protection_target.data_gap_skipped",)


def test_rejection_best_effort_ids_are_deterministic_and_bounded(bar: Candle) -> None:
    malformed = Feature(
        key="entry_model.entry_plan",
        source_module="entry_model",
        created_at=bar.close_time,
        value={
            "entry_plan_id": ["not", "an", "id"],
            "setup_id": {"not": "an id"},
            "symbol": bar.symbol,
            "timeframe": bar.timeframe,
            "side": "buy",
            "entry_price": "100",
        },
        symbol=bar.symbol,
        timeframe=bar.timeframe,
        valid_until=bar.close_time,
        feature_id="malformed-deterministic-id",
    )
    decision = ProtectionTargetModule().on_bar(make_view(bar, malformed))
    rejection = decision.features[0].value
    assert rejection["entry_plan_id"] is None
    assert rejection["setup_id"] is None
    assert "0x" not in str(rejection)
