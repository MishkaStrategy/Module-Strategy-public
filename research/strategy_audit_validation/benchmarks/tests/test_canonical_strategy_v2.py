from __future__ import annotations

import csv
import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

_PANDAS_AVAILABLE = importlib.util.find_spec("pandas") is not None
pytestmark = pytest.mark.skipif(
    not _PANDAS_AVAILABLE,
    reason="canonical benchmark execution requires the dedicated pandas backtest environment",
)

if _PANDAS_AVAILABLE:
    import pandas as pd

    _BENCHMARK_ROOT = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(_BENCHMARK_ROOT))
    from benchmarks import _common as common
    from benchmarks import _portfolio as portfolio
    from benchmarks import _signals as signals
else:
    pd = common = portfolio = signals = None


def _bars(rows: list[tuple[float, float, float, float]]) -> Any:
    assert pd is not None
    opened = pd.date_range("2026-01-01", periods=len(rows), freq="5min", tz="UTC")
    return pd.DataFrame(
        {
            "open_time": opened,
            "open": [row[0] for row in rows],
            "high": [row[1] for row in rows],
            "low": [row[2] for row in rows],
            "close": [row[3] for row in rows],
            "volume": [1.0] * len(rows),
            "close_time": opened + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1),
        }
    )


def _signal(*, side: str = "long", priority: int = 1, available_index: int = 0) -> Any:
    assert pd is not None and common is not None
    available_at = pd.Timestamp("2026-01-01", tz="UTC") + pd.Timedelta(minutes=5 * available_index)
    return common.Signal(
        available_at=available_at,
        module_id="fvg_rt_final_15m" if priority == 1 else "frozen_1h_short",
        module_version="2.0.0" if priority == 1 else "1.0.0",
        setup_id="long" if side == "long" else "short",
        side=side,
        priority=priority,
        entry=100.0,
        stop=95.0 if side == "long" else 105.0,
        target_r=2.0,
        ttl_bars=3,
        max_hold_bars=5,
        no_progress_bars=2,
        no_progress_min_r=0.2,
        signal_key=f"{priority}" * 64,
    )


def test_aggregate_drops_incomplete_edge_periods() -> None:
    assert pd is not None and common is not None
    frame = _bars([(100, 101, 99, 100)] * 7)
    frame.loc[:, "open_time"] = pd.date_range("2026-01-01T00:05:00Z", periods=7, freq="5min")
    frame.loc[:, "close_time"] = (
        frame["open_time"] + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1)
    )

    aggregated = common.aggregate(frame, "15min", 3)

    assert list(aggregated.index) == [pd.Timestamp("2026-01-01T00:15:00Z")]
    assert aggregated.iloc[0].to_dict() == {
        "open": 100.0,
        "high": 101.0,
        "low": 99.0,
        "close": 100.0,
        "volume": 3.0,
    }


def test_long_target_and_fee_ledger_are_deterministic() -> None:
    assert pd is not None and portfolio is not None
    frame = _bars(
        [
            (101, 102, 99, 100),
            (100, 104, 99, 103),
            (103, 111, 102, 110),
        ]
    )

    first, counts = portfolio.run_portfolio(frame, [_signal()])
    second, _ = portfolio.run_portfolio(frame, [_signal()])

    pd.testing.assert_frame_equal(first, second)
    assert counts == {
        "fvg_rt_final_15m": 1,
        "frozen_1h_short": 0,
        "frozen_4h_short": 0,
    }
    assert len(first) == 1
    trade = first.iloc[0]
    assert trade["exit_reason"] == "target"
    assert trade["target_price"] == 110.0
    assert trade["exit_price"] == 110.0
    assert trade["net_pnl"] > 0


def test_stop_has_priority_when_stop_and_target_share_bar() -> None:
    assert portfolio is not None
    frame = _bars(
        [
            (100, 101, 99, 100),
            (100, 111, 94, 100),
        ]
    )

    ledger, _ = portfolio.run_portfolio(frame, [_signal()])

    assert ledger.iloc[0]["exit_reason"] == "stop"
    assert ledger.iloc[0]["exit_price"] < 95.0


def test_target_is_disabled_on_fill_bar_but_stop_is_active() -> None:
    assert portfolio is not None
    target_only = _bars([(100, 111, 99, 110), (109, 109, 108, 108.5)])
    stopped = _bars([(100, 111, 94, 100)])

    target_ledger, _ = portfolio.run_portfolio(target_only, [_signal()])
    stop_ledger, _ = portfolio.run_portfolio(stopped, [_signal()])

    assert target_ledger.iloc[0]["exit_reason"] == "end_of_test"
    assert stop_ledger.iloc[0]["exit_reason"] == "stop_on_fill_bar"


def test_higher_priority_preempts_lower_pending_before_fill() -> None:
    assert portfolio is not None
    frame = _bars(
        [
            (110, 111, 109, 110),
            (110, 111, 99, 100),
            (100, 111, 99, 110),
        ]
    )
    lower = replace(_signal(side="short", priority=2, available_index=0), entry=120.0, stop=125.0)
    higher = _signal(side="long", priority=1, available_index=1)

    ledger, counts = portfolio.run_portfolio(frame, [lower, higher])

    assert counts["frozen_1h_short"] == 1
    assert counts["fvg_rt_final_15m"] == 1
    assert ledger.iloc[0]["module_id"] == "fvg_rt_final_15m"


def test_pending_order_expires_before_late_touch() -> None:
    assert portfolio is not None
    signal = replace(_signal(), ttl_bars=2)
    frame = _bars(
        [
            (110, 111, 109, 110),
            (110, 111, 109, 110),
            (100, 101, 99, 100),
        ]
    )

    ledger, _ = portfolio.run_portfolio(frame, [signal])

    assert ledger.empty


def test_no_progress_excludes_fill_bar_mfe() -> None:
    assert portfolio is not None
    frame = _bars(
        [
            (100, 120, 99, 110),
            (110, 101, 99, 100),
            (100, 101, 99, 100),
        ]
    )

    ledger, _ = portfolio.run_portfolio(frame, [replace(_signal(), no_progress_min_r=0.21)])

    assert ledger.iloc[0]["exit_reason"] == "no_progress"
    assert ledger.iloc[0]["mfe_r"] == pytest.approx(0.2)


def test_signal_sorting_and_indicator_generation_are_stable() -> None:
    assert common is not None and signals is not None
    rows = []
    price = 100.0
    for index in range(600):
        drift = ((index % 17) - 8) * 0.01
        opened = price
        closed = max(1.0, opened + drift)
        rows.append((opened, max(opened, closed) + 0.2, min(opened, closed) - 0.2, closed))
        price = closed
    frame = _bars(rows)

    generated = signals.build_signals(frame)
    indicators = common.add_indicators(common.aggregate(frame, "15min", 3))

    assert generated == sorted(
        generated,
        key=lambda signal: (
            signal.available_at,
            signal.priority,
            {"long": 0, "short_a": 1, "short_b": 2, "short": 0}[signal.setup_id],
            signal.module_id,
            signal.signal_key,
        ),
    )
    assert indicators["atr14"].notna().any()
    assert indicators["ema50"].notna().any()


def test_ledger_csv_round_trips_float64_values(tmp_path: Path) -> None:
    assert pd is not None and portfolio is not None
    expected = {
        "equity_after": 15266.31855203826,
        "net_pnl": -0.12345678901234566,
        "mfe_r": 1.2345678901234567,
    }
    frame = pd.DataFrame([expected])
    output = tmp_path / "ledger.csv"

    portfolio._write_csv(frame, output)

    with output.open(newline="", encoding="utf-8") as handle:
        row = next(csv.DictReader(handle))
    assert {key: float(row[key]) for key in expected} == expected
