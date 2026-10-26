import pytest
from modular_core import Feature, SetupStatus

from strategy_modules.breakout_return import BreakoutReturnDataError, BreakoutReturnModule


def test_feature_range_arms_without_rolling_warmup(make_bar, make_view, range_feature):
    module = BreakoutReturnModule(reference_mode="feature")
    breakout = make_bar(1, 101, 104, 100.5, 103)
    decision = module.on_bar(make_view([breakout], features=(range_feature,)))
    assert decision.setups[0].status is SetupStatus.ARMED
    assert decision.setups[0].metadata["reference_id"] == "range-1"


def test_invalid_feature_schema_raises(make_bar, make_view):
    bar = make_bar(1, 101, 104, 100.5, 103)
    bad = Feature(
        key="range.current",
        source_module="range_builder",
        created_at=bar.close_time,
        value={"upper": 100},
        symbol=bar.symbol,
    )
    module = BreakoutReturnModule(reference_mode="feature")
    with pytest.raises(BreakoutReturnDataError, match="requires high and low"):
        module.on_bar(make_view([bar], features=(bad,)))


def test_reference_change_invalidates_active(make_bar, make_view, range_feature):
    module = BreakoutReturnModule(reference_mode="feature")
    breakout = make_bar(1, 101, 104, 100.5, 103)
    module.on_bar(make_view([breakout], features=(range_feature,)))
    changed = Feature(
        key="range.current",
        source_module="range_builder",
        created_at=make_bar(2, 103, 105, 102, 104).close_time,
        value={"high": 101, "low": 91, "range_id": "range-2"},
        symbol="BTCUSDT",
        feature_id="range-2",
    )
    next_bar = make_bar(2, 103, 105, 102, 104)
    decision = module.on_bar(make_view([breakout, next_bar], features=(changed,)))
    assert decision.setups[0].status is SetupStatus.INVALIDATED
    assert decision.skips == ("breakout_return.reference_changed",)


def test_missing_reference_invalidates_active(make_bar, make_view, range_feature):
    module = BreakoutReturnModule(reference_mode="feature")
    breakout = make_bar(1, 101, 104, 100.5, 103)
    module.on_bar(make_view([breakout], features=(range_feature,)))
    next_bar = make_bar(2, 103, 105, 102, 104)
    decision = module.on_bar(make_view([breakout, next_bar], features=()))
    assert decision.skips == ("breakout_return.reference_missing",)


def test_keep_snapshot_survives_feature_rollover(make_bar, make_view, range_feature):
    module = BreakoutReturnModule(
        reference_mode="feature",
        range_change_policy="keep_snapshot",
    )
    breakout = make_bar(1, 101, 104, 100.5, 103)
    module.on_bar(make_view([breakout], features=(range_feature,)))
    return_bar = make_bar(2, 99.5, 101, 98.5, 99)
    decision = module.on_bar(make_view([breakout, return_bar], features=()))
    assert decision.signals
