from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

pd = pytest.importorskip("pandas")

_BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BENCHMARK_ROOT))
from benchmarks import _common as common  # noqa: E402
from benchmarks import _portfolio as portfolio  # noqa: E402


def _bars() -> object:
    opened = pd.date_range("2026-01-01", periods=4, freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": opened,
            "open": [110.0, 110.0, 100.0, 100.0],
            "high": [111.0, 111.0, 101.0, 111.0],
            "low": [109.0, 109.0, 99.0, 99.0],
            "close": [110.0, 110.0, 100.0, 110.0],
            "volume": [1.0] * 4,
            "close_time": opened + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1),
        }
    )


def _signal(*, available_index: int, ttl_bars: int, key: str) -> common.Signal:
    available_at = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * available_index)
    return common.Signal(
        available_at=available_at,
        module_id="fvg_rt_final_15m",
        module_version="2.0.0",
        setup_id="long",
        side="long",
        priority=1,
        entry=100.0,
        stop=95.0,
        target_r=2.0,
        ttl_bars=ttl_bars,
        max_hold_bars=5,
        no_progress_bars=2,
        no_progress_min_r=0.2,
        signal_key=key * 64,
    )


def test_expiry_is_processed_before_same_timestamp_signal() -> None:
    expired = replace(_signal(available_index=0, ttl_bars=2, key="a"), entry=90.0, stop=85.0)
    replacement = _signal(available_index=2, ttl_bars=2, key="b")

    ledger, counts = portfolio.run_portfolio(_bars(), [expired, replacement])

    assert counts["fvg_rt_final_15m"] == 2
    assert len(ledger) == 1
    assert ledger.iloc[0]["signal_key"] == replacement.signal_key
