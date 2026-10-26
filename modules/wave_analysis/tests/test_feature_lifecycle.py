from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from modular_core import Candle

from tests.support import market_view
from wave_analysis import WaveAnalysisModule
from wave_analysis.contracts import CONFIRMED_PIVOTS_FEATURE, WAVE_LEGS_FEATURE

START = datetime(2026, 7, 1, tzinfo=UTC)
PERSISTENT_KEYS = {CONFIRMED_PIVOTS_FEATURE, WAVE_LEGS_FEATURE}


def series(count: int) -> list[Candle]:
    result: list[Candle] = []
    for index in range(count):
        price = 100.0 + (12.0 if index % 2 else -12.0) + index * 0.01
        result.append(
            Candle(
                START + timedelta(minutes=index),
                price,
                price + 1,
                price - 1,
                price,
                100,
                "BTCUSDT",
                "1m",
            )
        )
    return result


def module() -> WaveAnalysisModule:
    return WaveAnalysisModule(
        timeframes=("1m",),
        primary_timeframe="1m",
        pivot_method="percentage_zigzag",
        reversal_threshold="0.05",
    )


def test_persistent_feature_snapshots_are_superseded_not_accumulated() -> None:
    instance = module()
    history: list[Candle] = []
    active: dict[str, object] = {}
    deletion_count = 0

    for index, item in enumerate(series(80)):
        history.append(item)
        decision = instance.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))
        # Core publishes first, then applies exact-ID deletes.
        for feature in decision.features:
            assert feature.feature_id is not None
            active[feature.feature_id] = feature
        for deletion in decision.feature_deletes:
            assert deletion.feature_id is not None
            deletion_count += 1
            active.pop(deletion.feature_id, None)
        retained = [feature for feature in active.values() if feature.key in PERSISTENT_KEYS]
        assert len(retained) <= len(PERSISTENT_KEYS)

    assert deletion_count > 0
    assert {feature.key for feature in active.values() if feature.key in PERSISTENT_KEYS} == PERSISTENT_KEYS


def test_feature_supersession_identity_survives_checkpoint_restart() -> None:
    candles = series(30)
    first = module()
    history: list[Candle] = []
    for index, item in enumerate(candles[:20]):
        history.append(item)
        first.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))

    restored = module()
    restored.restore_state(first.snapshot_state())
    history.append(candles[20])
    first_decision = first.on_bar(market_view(tuple(history), {"1m": tuple(history)}, 20))
    restored_decision = restored.on_bar(market_view(tuple(history), {"1m": tuple(history)}, 20))

    assert [(item.key, item.feature_id) for item in first_decision.features] == [
        (item.key, item.feature_id) for item in restored_decision.features
    ]
    assert [(item.key, item.feature_id) for item in first_decision.feature_deletes] == [
        (item.key, item.feature_id) for item in restored_decision.feature_deletes
    ]
    assert first.state_digest == restored.state_digest


def test_pre_supersession_checkpoint_shape_remains_restoreable() -> None:
    instance = module()
    history: list[Candle] = []
    for index, item in enumerate(series(10)):
        history.append(item)
        instance.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))
    snapshot = copy.deepcopy(dict(instance.snapshot_state()))
    timeframes = dict(snapshot["timeframes"])
    timeframe = dict(timeframes["1m"])
    timeframe.pop("published_feature_ids")
    timeframes["1m"] = timeframe
    snapshot["timeframes"] = timeframes

    restored = module()
    restored.restore_state(snapshot)
    assert restored.snapshot_state()["timeframes"]["1m"]["published_feature_ids"] == {}


def test_data_gap_invalidates_persistent_snapshots_for_core_and_direct_adapters() -> None:
    instance = module()
    history: list[Candle] = []
    active: dict[str, object] = {}
    for index, item in enumerate(series(15)):
        history.append(item)
        decision = instance.on_bar(market_view(tuple(history), {"1m": tuple(history)}, index))
        for feature in decision.features:
            assert feature.feature_id is not None
            active[feature.feature_id] = feature
        for deletion in decision.feature_deletes:
            assert deletion.feature_id is not None
            active.pop(deletion.feature_id, None)

    persistent_before = [item for item in active.values() if item.key in PERSISTENT_KEYS]
    assert {item.key for item in persistent_before} == PERSISTENT_KEYS
    assert all(item.invalidate_on_gap for item in persistent_before)

    gap_item = series(16)[-1]
    gap_history = [*history, gap_item]
    decision = instance.on_bar(
        market_view(tuple(gap_history), {"1m": tuple(gap_history)}, len(gap_history) - 1, data_gap=True)
    )
    assert {item.key for item in decision.feature_deletes} == PERSISTENT_KEYS
    assert {item.reason for item in decision.feature_deletes} == {"data_gap_invalidation"}
    for deletion in decision.feature_deletes:
        assert deletion.feature_id is not None
        active.pop(deletion.feature_id, None)
    assert not [item for item in active.values() if item.key in PERSISTENT_KEYS]
    assert instance.snapshot_state()["timeframes"]["1m"]["published_feature_ids"] == {}
