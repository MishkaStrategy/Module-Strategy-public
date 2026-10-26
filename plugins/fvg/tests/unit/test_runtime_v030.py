from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from modular_strategy_fvg import (
    ACTIVE_ZONE_KEY,
    CREATED_EVENT_KEY,
    ZONE_STATE_KEY,
    FvgModule,
    FvgStateError,
)

from conftest import bullish_triplet, view_for


class _CountingAdapter:
    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self._timestamp = datetime(2024, 1, 1, tzinfo=timezone.utc)
        self._close_time = self._timestamp + timedelta(minutes=15)

    def _read(self, name: str, value: object) -> object:
        self.counts[name] = self.counts.get(name, 0) + 1
        return value

    @property
    def symbol(self) -> str:
        return self._read("symbol", "BTCUSDT")  # type: ignore[return-value]

    @property
    def timeframe(self) -> str:
        return self._read("timeframe", "15m")  # type: ignore[return-value]

    @property
    def timestamp(self) -> datetime:
        return self._read("timestamp", self._timestamp)  # type: ignore[return-value]

    @property
    def close_time(self) -> datetime:
        return self._read("close_time", self._close_time)  # type: ignore[return-value]


class _FailingAdapter(_CountingAdapter):
    @property
    def timestamp(self) -> datetime:
        self.counts["timestamp"] = self.counts.get("timestamp", 0) + 1
        raise RuntimeError("unstable timestamp property")


def test_v030_runtime_creates_zone_and_schema4_checkpoint() -> None:
    module = FvgModule(timeframes=("15m",))

    decision = module.on_bar(view_for(bullish_triplet()))

    keys = [feature.key for feature in decision.features]
    assert ACTIVE_ZONE_KEY in keys
    assert CREATED_EVENT_KEY in keys
    assert ZONE_STATE_KEY in keys
    assert len(module.active_zones) == 1

    state = module.snapshot_state()
    assert state["state_schema_version"] == "4"
    assert state["module_version"] == "0.3.0"
    assert len(state["zone_lifecycle"]) == 1

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(state)
    assert restored.snapshot_state() == state


def test_empty_schema4_checkpoint_round_trip() -> None:
    module = FvgModule(timeframes=("15m",))
    state = module.snapshot_state()

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(state)

    assert restored.snapshot_state() == state
    assert restored.active_zones == ()
    assert restored.pre_candidates == ()


def test_schema3_checkpoint_migrates_fail_closed() -> None:
    source = FvgModule(timeframes=("15m",))
    source.on_bar(view_for(bullish_triplet()))
    current = source.snapshot_state()
    legacy = {
        "state_schema_version": "3",
        "module_version": "0.2.1",
        "config_fingerprint": source._v3_config_fingerprint,
        "bound_symbol": current["bound_symbol"],
        "last_processed": deepcopy(current["last_processed"]),
        "zones": deepcopy(current["zones"]),
    }

    restored = FvgModule(timeframes=("15m",))
    restored.restore_state(legacy)
    migrated = restored.snapshot_state()

    assert migrated["state_schema_version"] == "4"
    assert migrated["module_version"] == "0.3.0"
    assert len(migrated["zone_lifecycle"]) == 1
    assert restored.active_zones == source.active_zones


def test_schema3_checkpoint_rejects_incompatible_v030_config() -> None:
    baseline = FvgModule(timeframes=("15m",))
    state = {
        "state_schema_version": "3",
        "module_version": "0.2.1",
        "config_fingerprint": baseline._v3_config_fingerprint,
        "bound_symbol": None,
        "last_processed": [],
        "zones": [],
    }
    incompatible = FvgModule(
        timeframes=("15m",),
        gap_size_mode="absolute",
        min_gap_size="1",
    )

    with pytest.raises(FvgStateError, match="cannot migrate"):
        incompatible.restore_state(state)


def test_restore_is_atomic_on_invalid_schema4_payload() -> None:
    module = FvgModule(timeframes=("15m",))
    module.on_bar(view_for(bullish_triplet()))
    before = module.snapshot_state()
    invalid = deepcopy(before)
    invalid["zone_lifecycle"][0]["zone_id"] = "not-canonical"

    with pytest.raises(FvgStateError):
        module.restore_state(invalid)

    assert module.snapshot_state() == before


def test_feature_boundary_reads_adapter_identity_once() -> None:
    adapter = _CountingAdapter()

    identity = FvgModule._feature_bar_identity(  # noqa: SLF001
        adapter,  # type: ignore[arg-type]
        "test adapter",
    )

    assert identity == (
        "BTCUSDT",
        "15m",
        datetime(2024, 1, 1, tzinfo=timezone.utc),
        datetime(2024, 1, 1, 0, 15, tzinfo=timezone.utc),
    )
    assert adapter.counts == {
        "symbol": 1,
        "timeframe": 1,
        "timestamp": 1,
        "close_time": 1,
    }


def test_feature_boundary_canonicalizes_adapter_property_failure() -> None:
    adapter = _FailingAdapter()

    with pytest.raises(
        ValueError,
        match="test adapter candle attribute timestamp cannot be read",
    ):
        FvgModule._feature_bar_identity(  # noqa: SLF001
            adapter,  # type: ignore[arg-type]
            "test adapter",
        )
