from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone
import math

import pytest

from modular_core.audit import Journal, Metrics, calculate_metrics
from modular_core.extensions import ReconciliationReport
from modular_core.models import PositionSide, PositionSnapshot


@pytest.mark.parametrize("field", ["matched", "position_mismatch", "safe_halt_required"])
def test_round07_reconciliation_flags_require_bool(field: str) -> None:
    values: dict[str, object] = {
        "matched": True,
        "position_mismatch": False,
        "safe_halt_required": False,
    }
    values[field] = 1
    with pytest.raises(TypeError, match=f"{field} must be bool"):
        ReconciliationReport(**values)  # type: ignore[arg-type]


def test_round07_reconciliation_mismatch_requires_safe_halt() -> None:
    with pytest.raises(ValueError, match="safe halt"):
        ReconciliationReport(
            matched=False,
            missing_remote_order_ids=("order-1",),
            safe_halt_required=False,
        )


def test_round07_reconciliation_identity_outputs_are_nonblank_unique() -> None:
    with pytest.raises(ValueError, match="missing_remote_order_ids"):
        ReconciliationReport(
            matched=False,
            missing_remote_order_ids=("order-1", "order-1"),
            safe_halt_required=True,
        )
    with pytest.raises(ValueError, match="missing_local_client_order_ids"):
        ReconciliationReport(
            matched=False,
            missing_local_client_order_ids=(" ",),
            safe_halt_required=True,
        )


def test_round07_reconciliation_issue_count_is_stable() -> None:
    report = ReconciliationReport(
        matched=False,
        missing_local_order_ids=("remote-order",),
        missing_remote_order_ids=("local-order",),
        position_mismatch=True,
        safe_halt_required=True,
        missing_local_client_order_ids=("remote-client",),
    )
    assert report.issue_count == 4


NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _position_snapshot(**overrides: object) -> PositionSnapshot:
    values: dict[str, object] = {
        "id": "position-1",
        "side": PositionSide.LONG,
        "quantity": 1.0,
        "entry_price": 100.0,
        "entry_time": NOW,
        "stop_loss": 90.0,
        "take_profit": 110.0,
        "tag": "probe",
        "unrealized_pnl": 1.0,
    }
    values.update(overrides)
    return PositionSnapshot(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("side", "long", "side must be PositionSide"),
        ("quantity", math.nan, "quantity must be finite"),
        ("quantity", 0, "quantity must be positive"),
        ("entry_price", math.inf, "entry_price must be finite"),
        ("stop_loss", -1, "stop_loss must be positive"),
        ("unrealized_pnl", math.nan, "unrealized_pnl must be finite"),
        ("entry_fill_count", True, "entry_fill_count must be int"),
        ("exit_fill_count", -1, "exit_fill_count cannot be negative"),
    ],
)
def test_round08_position_snapshot_rejects_invalid_output(
    field: str,
    value: object,
    message: str,
) -> None:
    with pytest.raises((TypeError, ValueError), match=message):
        _position_snapshot(**{field: value})


def test_round08_position_snapshot_normalizes_time_and_remains_frozen() -> None:
    offset = datetime(2026, 1, 1, 3, tzinfo=timezone(timedelta(hours=3)))
    snapshot = _position_snapshot(entry_time=offset)
    assert snapshot.entry_time == NOW
    with pytest.raises(FrozenInstanceError):
        snapshot.quantity = 2  # type: ignore[misc]


def _metrics(**overrides: object) -> Metrics:
    values: dict[str, object] = {
        "initial_equity": 1_000.0,
        "final_equity": 1_120.0,
        "net_profit": 120.0,
        "return_pct": 12.0,
        "trades": 4,
        "wins": 2,
        "losses": 1,
        "breakevens": 1,
        "win_rate": 50.0,
        "gross_profit": 200.0,
        "gross_loss": 80.0,
        "profit_factor": 2.5,
        "expectancy": 30.0,
        "average_win": 100.0,
        "average_loss": -80.0,
        "payoff_ratio": 1.25,
        "max_drawdown": 50.0,
        "max_drawdown_pct": 5.0,
        "max_consecutive_wins": 2,
        "max_consecutive_losses": 1,
        "average_r_multiple": 0.75,
        "average_holding_bars": 12.0,
        "exposure_pct": 40.0,
        "total_fees": 4.0,
    }
    values.update(overrides)
    return Metrics(**values)  # type: ignore[arg-type]


def test_metrics_accepts_consistent_aggregate_and_remains_frozen() -> None:
    metrics = _metrics()

    assert metrics.profit_factor == 2.5
    with pytest.raises(FrozenInstanceError):
        metrics.trades = 5  # type: ignore[misc]


def test_calculate_metrics_empty_journal_satisfies_contract() -> None:
    metrics = calculate_metrics(Journal("run-empty-metrics"), 1_000.0)

    assert metrics.trades == 0
    assert metrics.net_profit == 0.0
    assert metrics.profit_factor == 0.0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("trades", True),
        ("wins", 1.0),
        ("losses", "1"),
        ("breakevens", -1),
        ("max_consecutive_wins", True),
        ("max_consecutive_losses", -1),
    ],
)
def test_metrics_rejects_invalid_count_fields(field: str, value: object) -> None:
    with pytest.raises((TypeError, ValueError), match=field):
        _metrics(**{field: value})


def test_metrics_requires_trade_counts_to_reconcile() -> None:
    with pytest.raises(ValueError, match="must equal trades"):
        _metrics(breakevens=0)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_consecutive_wins", 0),
        ("max_consecutive_wins", 3),
        ("max_consecutive_losses", 0),
        ("max_consecutive_losses", 2),
    ],
)
def test_metrics_rejects_impossible_streaks(field: str, value: int) -> None:
    with pytest.raises(ValueError, match=field):
        _metrics(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "initial_equity",
        "final_equity",
        "net_profit",
        "return_pct",
        "win_rate",
        "gross_profit",
        "gross_loss",
        "expectancy",
        "average_win",
        "average_loss",
        "max_drawdown",
        "max_drawdown_pct",
        "exposure_pct",
        "total_fees",
    ],
)
def test_metrics_rejects_nonfinite_finite_fields(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        _metrics(**{field: math.nan})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("gross_profit", -1.0),
        ("gross_loss", -1.0),
        ("profit_factor", -1.0),
        ("average_win", -1.0),
        ("average_loss", 1.0),
        ("payoff_ratio", -1.0),
        ("max_drawdown", -1.0),
        ("max_drawdown_pct", -1.0),
        ("total_fees", -1.0),
    ],
)
def test_metrics_rejects_invalid_signs(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        _metrics(**{field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("win_rate", -0.1),
        ("win_rate", 100.1),
        ("exposure_pct", -0.1),
        ("exposure_pct", 100.1),
    ],
)
def test_metrics_rejects_invalid_percent_ranges(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        _metrics(**{field: value})


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("net_profit", 121.0, "net_profit identity"),
        ("return_pct", 11.0, "return_pct identity"),
        ("win_rate", 25.0, "win_rate identity"),
        ("gross_profit", 199.0, "gross_profit identity"),
        ("gross_loss", 79.0, "gross_loss identity"),
        ("expectancy", 29.0, "expectancy identity"),
        ("profit_factor", 2.0, "profit_factor identity"),
        ("payoff_ratio", 1.0, "payoff_ratio identity"),
    ],
)
def test_metrics_rejects_inconsistent_aggregates(field: str, value: float, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _metrics(**{field: value})


def test_metrics_allows_infinite_ratios_only_when_denominator_is_zero() -> None:
    metrics = _metrics(
        final_equity=1_200.0,
        net_profit=200.0,
        return_pct=20.0,
        trades=2,
        wins=2,
        losses=0,
        breakevens=0,
        win_rate=100.0,
        gross_profit=200.0,
        gross_loss=0.0,
        profit_factor=math.inf,
        expectancy=100.0,
        average_win=100.0,
        average_loss=0.0,
        payoff_ratio=math.inf,
        max_consecutive_wins=2,
        max_consecutive_losses=0,
    )

    assert metrics.profit_factor == math.inf
    assert metrics.payoff_ratio == math.inf

    with pytest.raises(ValueError, match="profit_factor identity"):
        _metrics(profit_factor=math.inf)
    with pytest.raises(ValueError, match="payoff_ratio identity"):
        _metrics(payoff_ratio=math.inf)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("average_r_multiple", math.nan),
        ("average_holding_bars", math.inf),
        ("average_holding_bars", -1.0),
    ],
)
def test_metrics_rejects_invalid_optional_averages(field: str, value: float) -> None:
    with pytest.raises(ValueError, match=field):
        _metrics(**{field: value})


def test_metrics_tolerates_normal_floating_point_roundoff() -> None:
    _metrics(
        final_equity=1_000.3,
        net_profit=0.1 + 0.2,
        return_pct=(0.1 + 0.2) / 1_000.0 * 100,
        trades=3,
        wins=2,
        losses=1,
        breakevens=0,
        win_rate=2 / 3 * 100,
        gross_profit=0.4,
        gross_loss=0.1,
        profit_factor=4.0,
        expectancy=(0.4 - 0.1) / 3,
        average_win=0.2,
        average_loss=-0.1,
        payoff_ratio=2.0,
        max_consecutive_wins=2,
        max_consecutive_losses=1,
    )
