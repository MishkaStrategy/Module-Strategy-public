from __future__ import annotations

from datetime import UTC, datetime, timedelta

from modular_backtest.scalability.manifest import (
    build_scalable_run_manifest,
    incremental_candle_data_hash,
)
from modular_core.config import DataConfig, EngineConfig
from modular_core.manifest import ModuleManifest
from modular_core.models import Candle
from modular_core.run_manifest import build_run_manifest


def _bars() -> list[Candle]:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    return [
        Candle(
            timestamp=start + timedelta(minutes=5 * index),
            open=100.0 + index,
            high=101.0 + index,
            low=99.0 + index,
            close=100.5 + index,
            volume=10.0 + index,
            symbol="BTCUSDT",
            timeframe="5m",
        )
        for index in range(7)
    ]


def test_incremental_hash_matches_core_manifest_hash() -> None:
    bars = _bars()
    manifest = ModuleManifest(name="noop", version="0.1.0", api_version="1.0")
    config = EngineConfig(data=DataConfig(expected_interval_seconds=300))
    created_at = datetime(2024, 2, 1, tzinfo=UTC)

    legacy = build_run_manifest(
        kernel_version="0.4.2",
        created_at=created_at,
        bars=bars,
        config=config,
        manifests=(manifest,),
    )
    scalable = build_scalable_run_manifest(
        kernel_version="0.4.2",
        created_at=created_at,
        bars=bars,
        config=config,
        manifests=(manifest,),
    )

    assert incremental_candle_data_hash(bars) == legacy.data_hash
    assert scalable == legacy


def test_incremental_hash_normalizes_negative_zero_like_core() -> None:
    bar = Candle(
        timestamp=datetime(2024, 1, 1, tzinfo=UTC),
        open=1.0,
        high=2.0,
        low=0.5,
        close=1.5,
        volume=-0.0,
        symbol="BTCUSDT",
        timeframe="5m",
    )
    manifest = ModuleManifest(name="noop", version="0.1.0", api_version="1.0")
    config = EngineConfig(data=DataConfig(expected_interval_seconds=300))
    legacy = build_run_manifest(
        kernel_version="0.4.2",
        created_at=bar.timestamp,
        bars=[bar],
        config=config,
        manifests=(manifest,),
    )

    assert incremental_candle_data_hash([bar]) == legacy.data_hash
