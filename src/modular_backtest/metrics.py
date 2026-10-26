from __future__ import annotations

import math
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from statistics import median
from typing import Protocol


class TradeLike(Protocol):
    @property
    def side(self) -> object: ...

    @property
    def entry_time(self) -> datetime: ...

    @property
    def exit_time(self) -> datetime: ...

    @property
    def net_pnl(self) -> float: ...

    @property
    def holding_bars(self) -> int | None: ...


class FillLike(Protocol):
    @property
    def fee(self) -> float: ...

    @property
    def slippage(self) -> float: ...

    @property
    def quantity(self) -> float: ...


class EquityLike(Protocol):
    @property
    def timestamp(self) -> datetime: ...

    @property
    def cash(self) -> float: ...

    @property
    def equity(self) -> float: ...

    @property
    def position_open(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class DrawdownSummary:
    absolute: float
    percent: float


def _drawdown(values: Sequence[float], initial_equity: float) -> DrawdownSummary:
    peak = initial_equity
    maximum = 0.0
    maximum_pct = 0.0
    for value in values:
        peak = max(peak, value)
        amount = peak - value
        maximum = max(maximum, amount)
        if peak > 0:
            maximum_pct = max(maximum_pct, amount / peak * 100)
    return DrawdownSummary(maximum, maximum_pct)


def _side_name(side: object) -> str:
    value = getattr(side, "value", side)
    return str(value)


def calculate_extended_metrics(
    *,
    trades: Sequence[TradeLike],
    fills: Sequence[FillLike],
    equity: Sequence[EquityLike],
    initial_equity: float,
    contract_size: float = 1.0,
) -> dict[str, object]:
    if (
        isinstance(contract_size, bool)
        or not isinstance(contract_size, int | float)
        or not math.isfinite(float(contract_size))
        or contract_size <= 0
    ):
        raise ValueError("contract_size must be a finite positive number")
    normalized_contract_size = float(contract_size)

    pnls = [trade.net_pnl for trade in trades]
    wins = sum(value > 0 for value in pnls)
    losses = sum(value < 0 for value in pnls)
    gross_profit = sum(value for value in pnls if value > 0)
    gross_loss = abs(sum(value for value in pnls if value < 0))
    final_equity = equity[-1].equity if equity else initial_equity
    closed_dd = _drawdown([point.cash for point in equity], initial_equity)
    mark_dd = _drawdown([point.equity for point in equity], initial_equity)
    holding = [trade.holding_bars for trade in trades if trade.holding_bars is not None]

    side_rows: dict[str, dict[str, float | int]] = {}
    for side in sorted({_side_name(trade.side) for trade in trades}):
        selected = [trade.net_pnl for trade in trades if _side_name(trade.side) == side]
        side_rows[side] = {
            "trades": len(selected),
            "net_pnl": sum(selected),
            "wins": sum(value > 0 for value in selected),
            "losses": sum(value < 0 for value in selected),
        }

    monthly: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0}
    )
    yearly: dict[str, dict[str, float | int]] = defaultdict(
        lambda: {"trades": 0, "net_pnl": 0.0, "wins": 0, "losses": 0}
    )
    for trade in trades:
        for key, target in (
            (trade.exit_time.strftime("%Y-%m"), monthly),
            (trade.exit_time.strftime("%Y"), yearly),
        ):
            row = target[key]
            row["trades"] = int(row["trades"]) + 1
            row["net_pnl"] = float(row["net_pnl"]) + trade.net_pnl
            row["wins"] = int(row["wins"]) + int(trade.net_pnl > 0)
            row["losses"] = int(row["losses"]) + int(trade.net_pnl < 0)

    return {
        "definitions": {
            "closed_equity": "portfolio cash after realised fills and fees",
            "mark_to_market_equity": "cash plus unrealised PnL at every processed bar",
            "monthly_and_yearly_results": "trades grouped by UTC exit timestamp",
            "commissions": "sum of fees on every fill, including fills for positions left open",
            "slippage": (
                "sum of per-unit adverse execution adjustments multiplied by fill quantity "
                "and instrument contract size"
            ),
        },
        "initial_equity": initial_equity,
        "final_equity": final_equity,
        "net_pnl": final_equity - initial_equity,
        "total_return_pct": ((final_equity / initial_equity) - 1) * 100,
        "compounded_return_pct": ((final_equity / initial_equity) - 1) * 100,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "profit_factor": gross_profit / gross_loss if gross_loss else None,
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": wins / len(trades) * 100 if trades else 0.0,
        "loss_rate_pct": losses / len(trades) * 100 if trades else 0.0,
        "average_trade": sum(pnls) / len(pnls) if pnls else 0.0,
        "median_trade": median(pnls) if pnls else 0.0,
        "expectancy": sum(pnls) / len(pnls) if pnls else 0.0,
        "max_closed_equity_drawdown": {
            "absolute": closed_dd.absolute,
            "percent": closed_dd.percent,
        },
        "max_mark_to_market_drawdown": {
            "absolute": mark_dd.absolute,
            "percent": mark_dd.percent,
        },
        "average_holding_bars": sum(holding) / len(holding) if holding else None,
        "maximum_holding_bars": max(holding) if holding else None,
        "exposure_time_pct": (
            sum(point.position_open for point in equity) / len(equity) * 100 if equity else 0.0
        ),
        "total_commissions": sum(fill.fee for fill in fills),
        "total_slippage": sum(
            fill.slippage * fill.quantity * normalized_contract_size for fill in fills
        ),
        "long_short_breakdown": side_rows,
        "monthly_results": dict(sorted(monthly.items())),
        "yearly_results": dict(sorted(yearly.items())),
    }
