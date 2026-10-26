from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from modular_backtest.metrics import calculate_extended_metrics


def test_extended_metrics_separate_closed_and_mark_to_market_drawdown() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    trades = [
        SimpleNamespace(
            side=SimpleNamespace(value="long"),
            entry_time=start,
            exit_time=start + timedelta(hours=1),
            net_pnl=100.0,
            holding_bars=12,
        ),
        SimpleNamespace(
            side=SimpleNamespace(value="short"),
            entry_time=start + timedelta(days=32),
            exit_time=start + timedelta(days=32, hours=2),
            net_pnl=-50.0,
            holding_bars=24,
        ),
    ]
    fills = [SimpleNamespace(fee=5.0, slippage=0.5, quantity=2.0)]
    equity = [
        SimpleNamespace(timestamp=start, cash=1000.0, equity=1000.0, position_open=False),
        SimpleNamespace(
            timestamp=start + timedelta(minutes=5), cash=1000.0, equity=900.0, position_open=True
        ),
        SimpleNamespace(
            timestamp=start + timedelta(hours=1), cash=1100.0, equity=1100.0, position_open=False
        ),
        SimpleNamespace(
            timestamp=start + timedelta(days=32, hours=2),
            cash=1050.0,
            equity=1050.0,
            position_open=False,
        ),
    ]
    metrics = calculate_extended_metrics(
        trades=trades, fills=fills, equity=equity, initial_equity=1000.0
    )
    assert metrics["net_pnl"] == 50.0
    assert metrics["median_trade"] == 25.0
    assert metrics["profit_factor"] == 2.0
    assert metrics["maximum_holding_bars"] == 24
    assert metrics["total_commissions"] == 5.0
    assert metrics["total_slippage"] == 1.0
    assert metrics["max_closed_equity_drawdown"]["absolute"] == 50.0
    assert metrics["max_mark_to_market_drawdown"]["absolute"] == 100.0
    assert metrics["monthly_results"]["2024-01"]["net_pnl"] == 100.0
    assert metrics["monthly_results"]["2024-02"]["net_pnl"] == -50.0
    assert metrics["long_short_breakdown"]["long"]["trades"] == 1


def test_commissions_include_fills_for_a_position_left_open() -> None:
    start = datetime(2024, 1, 1, tzinfo=UTC)
    metrics = calculate_extended_metrics(
        trades=[],
        fills=[SimpleNamespace(fee=1.25, slippage=0.0, quantity=2.0)],
        equity=[
            SimpleNamespace(
                timestamp=start,
                cash=998.75,
                equity=1001.0,
                position_open=True,
            )
        ],
        initial_equity=1000.0,
    )

    assert metrics["trades"] == 0
    assert metrics["total_commissions"] == 1.25
    assert metrics["exposure_time_pct"] == 100.0


def test_empty_extended_metrics_are_defined() -> None:
    metrics = calculate_extended_metrics(trades=[], fills=[], equity=[], initial_equity=1000.0)
    assert metrics["final_equity"] == 1000.0
    assert metrics["profit_factor"] is None
    assert metrics["average_holding_bars"] is None
    assert metrics["exposure_time_pct"] == 0.0
