from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pandas as pd

from benchmarks._common import Signal, canonical_sha256
from benchmarks.modular_fvg_composition_parity import (
    build_runtime_signal_plan,
    replay_runtime_producers,
)


def _signal_key(
    *,
    available_at: datetime,
    setup_id: str,
    entry: float,
    stop: float,
) -> str:
    return canonical_sha256(
        {
            "available_at": available_at.isoformat(),
            "entry": round(entry, 12),
            "module_id": "fvg_rt_final_15m",
            "module_version": "2.0.0",
            "setup_id": setup_id,
            "stop": round(stop, 12),
        }
    )


def _frame(available_at: datetime, *, close: float) -> pd.DataFrame:
    open_time = available_at - timedelta(minutes=5)
    return pd.DataFrame(
        [
            {
                "open_time": pd.Timestamp(open_time),
                "open": close,
                "high": max(close, 106.0),
                "low": min(close, 99.0),
                "close": close,
                "volume": 10.0,
                "close_time": pd.Timestamp(available_at - timedelta(milliseconds=1)),
            }
        ]
    )


def test_runtime_producer_replay_is_causal_on_a_flat_core_timeframe_stream() -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    frame = pd.DataFrame(
        [
            {
                "open_time": pd.Timestamp(start + timedelta(minutes=5 * index)),
                "open": 100.0,
                "high": 101.0,
                "low": 99.0,
                "close": 100.0,
                "volume": 10.0,
            }
            for index in range(600)
        ]
    )

    assert replay_runtime_producers(frame) == []


def test_single_signal_survives_real_modular_contract_chain() -> None:
    available_at = datetime(2026, 1, 2, 12, 5, tzinfo=UTC)
    signal = Signal(
        available_at=pd.Timestamp(available_at),
        module_id="fvg_rt_final_15m",
        module_version="2.0.0",
        setup_id="long",
        side="long",
        priority=1,
        entry=100.1,
        stop=99.8,
        target_r=4.5,
        ttl_bars=36,
        max_hold_bars=288,
        no_progress_bars=48,
        no_progress_min_r=0.15,
        signal_key=_signal_key(
            available_at=available_at,
            setup_id="long",
            entry=100.1,
            stop=99.8,
        ),
    )

    runtime, evidence = build_runtime_signal_plan(
        _frame(available_at, close=105.0),
        [signal],
    )

    assert runtime == [signal]
    assert evidence == {"signal_groups": 1, "multi_candidate_groups": 0}


def test_final15_same_bar_tie_break_matches_historical_setup_order() -> None:
    available_at = datetime(2026, 1, 2, 12, 5, tzinfo=UTC)
    short_a = Signal(
        available_at=pd.Timestamp(available_at),
        module_id="fvg_rt_final_15m",
        module_version="2.0.0",
        setup_id="short_a",
        side="short",
        priority=1,
        entry=101.0,
        stop=102.0,
        target_r=5.0,
        ttl_bars=48,
        max_hold_bars=288,
        no_progress_bars=24,
        no_progress_min_r=0.35,
        signal_key=_signal_key(
            available_at=available_at,
            setup_id="short_a",
            entry=101.0,
            stop=102.0,
        ),
    )
    short_b = Signal(
        available_at=pd.Timestamp(available_at),
        module_id="fvg_rt_final_15m",
        module_version="2.0.0",
        setup_id="short_b",
        side="short",
        priority=1,
        entry=101.0,
        stop=103.0,
        target_r=6.0,
        ttl_bars=48,
        max_hold_bars=576,
        no_progress_bars=60,
        no_progress_min_r=0.35,
        signal_key=_signal_key(
            available_at=available_at,
            setup_id="short_b",
            entry=101.0,
            stop=103.0,
        ),
    )

    runtime, evidence = build_runtime_signal_plan(
        _frame(available_at, close=100.0),
        [short_a, short_b],
    )

    assert runtime == [short_a, short_b]
    assert evidence == {"signal_groups": 1, "multi_candidate_groups": 1}
