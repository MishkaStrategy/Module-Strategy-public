from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from modular_core import (
    BacktestEngine,
    Candle,
    Feature,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    StrategyModule,
)
from modular_core.features import FeatureStore
from modular_core.validation import utc_datetime


@pytest.mark.parametrize("value", [None, "2026-01-01", 123])
def test_round03_utc_boundary_rejects_non_datetime(value: object) -> None:
    with pytest.raises(TypeError, match="must be datetime"):
        utc_datetime(value, "timestamp")  # type: ignore[arg-type]


def test_round03_utc_boundary_normalizes_aware_offset() -> None:
    value = datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
    assert utc_datetime(value, "timestamp") == datetime(2026, 1, 1, tzinfo=timezone.utc)


def test_round04_feature_temporal_operations_reject_naive_timestamps() -> None:
    feature = Feature("probe.value", "probe", datetime(2026, 1, 1, tzinfo=timezone.utc), 1)
    store = FeatureStore()
    store.publish(feature)
    with pytest.raises(ValueError, match="timezone-aware"):
        feature.is_active(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.expire(datetime(2026, 1, 1))
    with pytest.raises(ValueError, match="timezone-aware"):
        store.snapshot(datetime(2026, 1, 1))


def test_round04_feature_snapshot_normalizes_equivalent_offsets_for_cache() -> None:
    store = FeatureStore()
    created = datetime(2026, 1, 1, tzinfo=timezone.utc)
    store.publish(Feature("probe.value", "probe", created, 1))
    first = store.snapshot(created)
    equivalent = datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
    second = store.snapshot(equivalent)
    assert second is first
    assert store.snapshot_builds == 1


class _FutureFeatureModule(StrategyModule):
    name = "future_feature_probe"
    manifest = ModuleManifest(name=name, produced_features=("future.value",))

    def on_bar(self, view: MarketView) -> ModuleDecision:
        return ModuleDecision(
            features=(
                Feature(
                    "future.value",
                    self.name,
                    view.current.close_time + timedelta(microseconds=1),
                    1,
                ),
            )
        )


def test_round04_engine_rejects_feature_after_completed_bar_boundary() -> None:
    bar = Candle(
        datetime(2026, 1, 1, tzinfo=timezone.utc),
        100,
        101,
        99,
        100,
        1,
        "X",
        "5m",
    )
    with pytest.raises(ValueError, match="future"):
        BacktestEngine(_FutureFeatureModule()).run((bar,))
