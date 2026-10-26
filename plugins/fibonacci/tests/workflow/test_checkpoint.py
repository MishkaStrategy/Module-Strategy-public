from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from modular_strategy_fibonacci.config import FibonacciConfig
from modular_strategy_fibonacci.contracts import Candle, WorkflowPhase
from modular_strategy_fibonacci.workflow.state_machine import SequentialFibonacciEngine


def candle(index: int, o: str, h: str, low: str, c: str) -> Candle:
    start = datetime(2025, 1, 1, tzinfo=timezone.utc) + timedelta(minutes=15 * index)
    return Candle(
        open_time=start,
        close_time=start + timedelta(minutes=15),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal("1"),
    )


def seeded() -> SequentialFibonacciEngine:
    engine = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-v1",
        config=FibonacciConfig(),
    )
    engine.seed_wave(
        direction="bullish",
        start_time=datetime(2024, 12, 31, 22, tzinfo=timezone.utc),
        start_price=Decimal("100"),
        end_time=datetime(2024, 12, 31, 23, tzinfo=timezone.utc),
        end_price=Decimal("150"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )
    return engine


def test_checkpoint_roundtrip_preserves_full_endpoint_and_continues_identically(tmp_path) -> None:
    uninterrupted = seeded()
    interrupted = seeded()
    prefix = (
        candle(0, "150", "160", "145", "158"),
        candle(1, "158", "170", "155", "168"),
    )
    suffix = (
        candle(2, "168", "169", "135", "140"),
        candle(3, "140", "182", "138", "180"),
    )
    for item in prefix:
        uninterrupted.process(item)
        interrupted.process(item)

    path = interrupted.save_checkpoint(tmp_path / "checkpoint.json")
    restored = SequentialFibonacciEngine.load_checkpoint(path, config=FibonacciConfig())
    assert restored.active_wave == interrupted.active_wave
    assert restored.active_wave is not None
    assert restored.active_wave.end_price == Decimal("170")
    assert restored.active_wave.endpoint_confirmed_at is None

    for item in suffix:
        expected = uninterrupted.process(item)
        actual = restored.process(item)
    assert actual == expected
    assert actual.active_wave is not None
    assert actual.active_wave.endpoint_confirmed_at is not None


def test_corrupted_checkpoint_is_rejected(tmp_path) -> None:
    path = tmp_path / "broken.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(ValueError, match="cannot read"):
        SequentialFibonacciEngine.load_checkpoint(path)


def test_checkpoint_with_mismatched_config_is_rejected(tmp_path) -> None:
    engine = seeded()
    engine.process(candle(0, "150", "160", "145", "158"))
    path = engine.save_checkpoint(tmp_path / "checkpoint.json")
    changed = FibonacciConfig(max_wait_correction_bars=17)
    with pytest.raises(ValueError, match="configuration mismatch"):
        SequentialFibonacciEngine.load_checkpoint(path, config=changed)


def test_checkpoint_rejects_inconsistent_sequence_number() -> None:
    engine = seeded()
    state = engine.snapshot_state()
    state["sequence_number"] = 0
    with pytest.raises(ValueError, match="sequence number"):
        SequentialFibonacciEngine.from_state(state, config=FibonacciConfig())


def test_checkpoint_rejects_unknown_keys() -> None:
    engine = seeded()
    state = engine.snapshot_state()
    state["unexpected"] = True
    with pytest.raises(ValueError, match="invalid checkpoint keys"):
        SequentialFibonacciEngine.from_state(state, config=FibonacciConfig())


def _deep_freeze_like_core(value):
    from types import MappingProxyType
    from collections.abc import Mapping

    if isinstance(value, Mapping):
        return MappingProxyType({key: _deep_freeze_like_core(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_deep_freeze_like_core(item) for item in value)
    return value


def test_checkpoint_restore_accepts_core_deep_frozen_payload() -> None:
    engine = SequentialFibonacciEngine(
        symbol="BTCUSDT",
        timeframe="15m",
        dataset_hash="dataset-frozen",
        config=FibonacciConfig(),
    )
    engine.seed_wave(
        direction="bullish",
        start_time=datetime(2025, 1, 1, tzinfo=timezone.utc),
        start_price=Decimal("100"),
        end_time=datetime(2025, 1, 1, 1, tzinfo=timezone.utc),
        end_price=Decimal("150"),
        phase=WorkflowPhase.AWAITING_CORRECTION,
    )
    frozen = _deep_freeze_like_core(engine.snapshot_state())
    restored = SequentialFibonacciEngine.from_state(frozen, config=FibonacciConfig())
    assert restored.snapshot_state() == engine.snapshot_state()
