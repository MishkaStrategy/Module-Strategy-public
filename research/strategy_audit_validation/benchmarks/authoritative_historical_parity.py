"""Authoritative Core/Backtest parity bridge for the immutable canonical Variant 2 baseline.

This module is research-only.  It converts the already-accepted historical signal
stream into public ``StrategyModule`` decisions and delegates all execution,
fees, sizing, slippage and portfolio economics to ``modular_backtest`` /
``modular_core``.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import MappingProxyType
from typing import Any, Final, cast

import pandas as pd
from modular_backtest.config import (
    BacktestAppConfig,
    CheckpointConfig,
    DataSourceConfig,
    OutputConfig,
)
from modular_backtest.runner import run_backtest
from modular_backtest.scalability import PerformanceConfig
from modular_core import (
    CancelRequest,
    ClosePositionRequest,
    MarketView,
    ModuleDecision,
    ModuleManifest,
    OrderRequest,
    OrderType,
    PositionSide,
    Side,
    StrategyModule,
    Trade,
)

from strategy_audit_validation.canonical_runtime import (
    MODULE_RULES,
    ContractBoundAdapter,
    SignalSnapshot,
)

from ._common import (
    ADVERSE_SLIPPAGE,
    CANDIDATE_ID,
    COMPOSITION_HASH,
    INITIAL_EQUITY,
    LEDGER_COLUMNS,
    MAKER_FEE,
    MARKET_FEE,
    MAX_NOTIONAL_MULTIPLE,
    RISK_FRACTION,
    Signal,
    canonical_sha256,
    load_5m,
)
from ._signals import build_signals
from .canonical_strategy_v2 import _load_provenance

MODULE_NAME: Final = "canonical_historical_parity"
REFERENCE_SEMANTIC_DIGEST: Final = (
    "63a2c102601cec5a613ff3749754d31955d6513c654c73cd3dd7e09bbc5cefdb"
)
_SETUP_PRIORITY: Final = {"long": 0, "short_a": 1, "short_b": 2, "short": 0}
_SHA256_RE: Final = re.compile(r"^[0-9a-f]{64}$")
_FIVE_MINUTES: Final = timedelta(minutes=5)


@dataclass(frozen=True, slots=True)
class CanonicalSignalPlan:
    available_at: datetime
    module_id: str
    module_version: str
    setup_id: str
    side: str
    priority: int
    entry: float
    stop: float
    target: float
    target_r: float
    ttl_bars: int
    max_hold_bars: int
    no_progress_bars: int
    no_progress_min_r: float
    signal_key: str

    def __post_init__(self) -> None:
        if self.available_at.tzinfo is None or self.available_at.utcoffset() != timedelta(0):
            raise ValueError("canonical signal availability must use UTC")
        if self.side not in {"long", "short"}:
            raise ValueError("canonical signal side must be long or short")
        if _SHA256_RE.fullmatch(self.signal_key) is None:
            raise ValueError("canonical signal key must be a lowercase SHA-256")
        for name in ("entry", "stop", "target", "target_r", "no_progress_min_r"):
            value = getattr(self, name)
            if not isinstance(value, int | float) or isinstance(value, bool):
                raise TypeError(f"{name} must be numeric")
            if not math.isfinite(float(value)):
                raise ValueError(f"{name} must be finite")


@dataclass(frozen=True, slots=True)
class RegisteredSignalPlan:
    digest: str
    signals: tuple[CanonicalSignalPlan, ...]
    by_time: Mapping[datetime, tuple[CanonicalSignalPlan, ...]]
    by_key: Mapping[str, CanonicalSignalPlan]


_PLAN_REGISTRY: dict[str, RegisteredSignalPlan] = {}


def _as_utc_datetime(value: object) -> datetime:
    if isinstance(value, pd.Timestamp):
        result = value.to_pydatetime()
    elif isinstance(value, datetime):
        result = value
    else:
        raise TypeError("canonical signal timestamp must be datetime-like")
    if result.tzinfo is None:
        raise ValueError("canonical signal timestamp must be timezone-aware")
    return result.astimezone(UTC)


def _plan_from_signal(signal: Signal) -> CanonicalSignalPlan:
    try:
        module_rules = MODULE_RULES[signal.module_id]
        setup_rules = module_rules.setups[signal.setup_id]
    except KeyError as exc:
        raise RuntimeError(
            "historical signal is not represented by canonical runtime rules"
        ) from exc
    if signal.module_version != module_rules.module_version:
        raise RuntimeError("historical signal module version drift")
    snapshot = SignalSnapshot(
        module_id=signal.module_id,
        module_version=signal.module_version,
        setup_id=signal.setup_id,
        signal_available_at=_as_utc_datetime(signal.available_at),
        first_eligible_5m_open=_as_utc_datetime(signal.available_at),
        conditions={name: True for name in setup_rules.required_conditions},
        completed_timeframes=setup_rules.required_completed_timeframes,
        entry_price=signal.entry,
        stop_price=signal.stop,
        semantic_input_digest=signal.signal_key,
    )
    runtime_plan = ContractBoundAdapter(signal.module_id).build_plan(snapshot)
    exact_contract = (
        signal.priority == runtime_plan.priority
        and signal.target_r == runtime_plan.target_r
        and signal.ttl_bars == runtime_plan.ttl_bars_5m
        and signal.max_hold_bars == runtime_plan.max_hold_bars_5m
        and signal.no_progress_bars == runtime_plan.no_progress_bars_5m
        and signal.no_progress_min_r == runtime_plan.no_progress_min_mfe_r
        and signal.side == runtime_plan.side
    )
    if not exact_contract:
        raise RuntimeError("historical signal execution contract drift")
    return CanonicalSignalPlan(
        available_at=_as_utc_datetime(signal.available_at),
        module_id=signal.module_id,
        module_version=signal.module_version,
        setup_id=signal.setup_id,
        side=signal.side,
        priority=signal.priority,
        entry=signal.entry,
        stop=signal.stop,
        target=runtime_plan.target_price,
        target_r=signal.target_r,
        ttl_bars=signal.ttl_bars,
        max_hold_bars=signal.max_hold_bars,
        no_progress_bars=signal.no_progress_bars,
        no_progress_min_r=signal.no_progress_min_r,
        signal_key=signal.signal_key,
    )


def _plan_payload(signal: CanonicalSignalPlan) -> dict[str, object]:
    return {
        "available_at": signal.available_at.isoformat(),
        "module_id": signal.module_id,
        "module_version": signal.module_version,
        "setup_id": signal.setup_id,
        "side": signal.side,
        "priority": signal.priority,
        "entry": signal.entry,
        "stop": signal.stop,
        "target": signal.target,
        "target_r": signal.target_r,
        "ttl_bars": signal.ttl_bars,
        "max_hold_bars": signal.max_hold_bars,
        "no_progress_bars": signal.no_progress_bars,
        "no_progress_min_r": signal.no_progress_min_r,
        "signal_key": signal.signal_key,
    }


def register_signal_plan(signals: Iterable[Signal]) -> RegisteredSignalPlan:
    normalized = tuple(_plan_from_signal(signal) for signal in signals)
    if len({signal.signal_key for signal in normalized}) != len(normalized):
        raise RuntimeError("canonical signal plan contains duplicate signal keys")
    digest = canonical_sha256([_plan_payload(signal) for signal in normalized])
    by_time_mutable: dict[datetime, list[CanonicalSignalPlan]] = defaultdict(list)
    for signal in normalized:
        by_time_mutable[signal.available_at].append(signal)
    by_time = MappingProxyType(
        {
            timestamp: tuple(values)
            for timestamp, values in sorted(by_time_mutable.items(), key=lambda item: item[0])
        }
    )
    by_key = MappingProxyType({signal.signal_key: signal for signal in normalized})
    registered = RegisteredSignalPlan(
        digest=digest,
        signals=normalized,
        by_time=by_time,
        by_key=by_key,
    )
    existing = _PLAN_REGISTRY.get(digest)
    if existing is not None and existing != registered:
        raise RuntimeError("signal plan digest collision")
    _PLAN_REGISTRY[digest] = registered
    return registered


def unregister_signal_plan(digest: str) -> None:
    _PLAN_REGISTRY.pop(digest, None)


def _select_signal(incoming: Sequence[CanonicalSignalPlan]) -> CanonicalSignalPlan:
    if not incoming:
        raise ValueError("cannot select from an empty signal set")
    return min(
        incoming,
        key=lambda signal: (
            signal.priority,
            _SETUP_PRIORITY[signal.setup_id],
            signal.module_id,
            signal.signal_key,
        ),
    )


def _position_mfe_r(signal: CanonicalSignalPlan, mfe_price: float) -> float:
    initial_r = signal.entry - signal.stop if signal.side == "long" else signal.stop - signal.entry
    if initial_r <= 0:
        raise RuntimeError("canonical signal has invalid risk geometry")
    value = (
        (mfe_price - signal.entry) / initial_r
        if signal.side == "long"
        else (signal.entry - mfe_price) / initial_r
    )
    return max(0.0, value)


class HistoricalParityModule(StrategyModule):
    """Causal public-Core adapter for the immutable historical signal stream."""

    name = MODULE_NAME
    manifest = ModuleManifest(
        name=MODULE_NAME,
        version="1.0.0",
        api_version="1.0",
        parameters_schema={
            "plan_digest": {
                "type": "string",
                "required": True,
            }
        },
        skip_until_ready=False,
        description="Research-only canonical historical parity adapter",
        state_schema_version="1",
    )

    def __init__(self, *, plan_digest: str) -> None:
        super().__init__()
        if _SHA256_RE.fullmatch(plan_digest) is None:
            raise ValueError("plan_digest must be a lowercase SHA-256")
        try:
            self._plan = _PLAN_REGISTRY[plan_digest]
        except KeyError as exc:
            raise RuntimeError("canonical signal plan was not registered in this process") from exc
        self.parameters = MappingProxyType({"plan_digest": plan_digest})
        self._position_signal_key: str | None = None
        self._fill_index: int | None = None
        self._mfe_price: float | None = None

    def _signal_from_tag(self, tag: str) -> CanonicalSignalPlan:
        try:
            return self._plan.by_key[tag]
        except KeyError as exc:
            raise RuntimeError(
                "Core position/order tag is outside the canonical signal plan"
            ) from exc

    @staticmethod
    def _order_request(signal: CanonicalSignalPlan) -> OrderRequest:
        return OrderRequest(
            side=Side.BUY if signal.side == "long" else Side.SELL,
            order_type=OrderType.LIMIT,
            price=signal.entry,
            stop_loss=signal.stop,
            take_profit=signal.target,
            risk_fraction=RISK_FRACTION,
            tag=signal.signal_key,
            client_order_id=f"historical-{signal.signal_key}",
            ttl_bars=signal.ttl_bars,
        )

    def _next_entry_decision(self, view: MarketView) -> ModuleDecision:
        next_open = view.current.close_time
        incoming = self._plan.by_time.get(next_open, ())
        if not incoming:
            return ModuleDecision()
        selected = _select_signal(incoming)
        pending = view.pending_orders
        cancellation: CancelRequest | None = None
        if len(pending) > 1:
            raise RuntimeError("historical parity requires at most one pending order")
        if pending:
            existing = self._signal_from_tag(pending[0].tag)
            expires_before_next_open = (
                next_open >= existing.available_at + _FIVE_MINUTES * existing.ttl_bars
            )
            higher_priority = selected.priority < existing.priority
            if not expires_before_next_open and not higher_priority:
                return ModuleDecision()
            cancellation = CancelRequest(
                order_id=pending[0].id,
                reason=(
                    "historical_expire_pre_submit"
                    if expires_before_next_open
                    else "historical_priority_replace"
                ),
            )
        return ModuleDecision(
            cancellations=((cancellation,) if cancellation is not None else ()),
            orders=(self._order_request(selected),),
        )

    def on_bar(self, view: MarketView) -> ModuleDecision:
        position = view.position
        if position is None:
            if self._position_signal_key is not None:
                self._clear_position_state()
            return self._next_entry_decision(view)

        signal = self._signal_from_tag(position.tag)
        expected_side = PositionSide.LONG if signal.side == "long" else PositionSide.SHORT
        if position.side is not expected_side:
            raise RuntimeError("Core position side differs from canonical signal")
        if position.entry_price != signal.entry:
            raise RuntimeError("Core entry price differs from canonical signal")

        if self._position_signal_key is None:
            self._position_signal_key = signal.signal_key
            self._fill_index = view.index
            self._mfe_price = signal.entry
        elif self._position_signal_key != signal.signal_key:
            raise RuntimeError("open position changed without a closing trade callback")

        if self._fill_index is None or self._mfe_price is None:
            raise RuntimeError("open canonical position state is incomplete")
        if view.index < self._fill_index:
            raise RuntimeError("canonical position fill index regressed")
        if view.index == self._fill_index:
            return ModuleDecision()

        self._mfe_price = (
            max(self._mfe_price, view.current.high)
            if signal.side == "long"
            else min(self._mfe_price, view.current.low)
        )
        bars_since_fill = view.index - self._fill_index
        close_reason: str | None = None
        if (
            bars_since_fill == signal.no_progress_bars
            and _position_mfe_r(signal, self._mfe_price) < signal.no_progress_min_r
        ):
            close_reason = "no_progress"
        elif bars_since_fill >= signal.max_hold_bars - 1:
            close_reason = "max_hold"
        if close_reason is None:
            return ModuleDecision()

        next_entry = self._next_entry_decision(view)
        return ModuleDecision(
            cancellations=next_entry.cancellations,
            closes=(ClosePositionRequest(reason=close_reason),),
            orders=next_entry.orders,
        )

    def on_trade(self, trade: Trade) -> None:
        if not trade.position_closed:
            raise RuntimeError("historical parity adapter does not support partial closes")
        if trade.tag not in self._plan.by_key:
            raise RuntimeError("closed Core trade is outside the canonical signal plan")
        if self._position_signal_key is not None and trade.tag != self._position_signal_key:
            raise RuntimeError("closed Core trade does not match adapter position state")
        self._clear_position_state()

    def snapshot_state(self) -> Mapping[str, Any]:
        return {
            "position_signal_key": self._position_signal_key,
            "fill_index": self._fill_index,
            "mfe_price": self._mfe_price,
        }

    def restore_state(self, state: Mapping[str, Any]) -> None:
        if not isinstance(state, Mapping):
            raise TypeError("historical parity module state must be a mapping")
        allowed = {"position_signal_key", "fill_index", "mfe_price"}
        if set(state) != allowed:
            raise ValueError("historical parity module state schema mismatch")
        signal_key = state["position_signal_key"]
        fill_index = state["fill_index"]
        mfe_price = state["mfe_price"]
        all_empty = signal_key is None and fill_index is None and mfe_price is None
        if all_empty:
            self._clear_position_state()
            return
        if (
            not isinstance(signal_key, str)
            or signal_key not in self._plan.by_key
            or not isinstance(fill_index, int)
            or isinstance(fill_index, bool)
            or fill_index < 0
            or not isinstance(mfe_price, int | float)
            or isinstance(mfe_price, bool)
            or not math.isfinite(float(mfe_price))
            or float(mfe_price) <= 0
        ):
            raise ValueError("historical parity module state is invalid")
        self._position_signal_key = signal_key
        self._fill_index = fill_index
        self._mfe_price = float(mfe_price)

    def _clear_position_state(self) -> None:
        self._position_signal_key = None
        self._fill_index = None
        self._mfe_price = None


def _write_module_config(path: Path, plan_digest: str) -> None:
    payload = {
        "modules": [
            {
                "class": f"{HistoricalParityModule.__module__}:HistoricalParityModule",
                "parameters": {"plan_digest": plan_digest},
            }
        ]
    }
    path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")


def _backtest_config(
    *,
    input_path: Path,
    module_config: Path,
    output_root: Path,
    checkpoint: bool,
) -> BacktestAppConfig:
    return BacktestAppConfig(
        data=DataSourceConfig(
            symbol="BTCUSDT",
            path=input_path,
            format="parquet",
            gap_policy="reject",
        ),
        modules=module_config,
        initial_capital=INITIAL_EQUITY,
        requested_timeframes=(),
        execution={
            "slippage_bps": ADVERSE_SLIPPAGE * 10_000,
            "stop_slippage_bps": ADVERSE_SLIPPAGE * 10_000,
            "intrabar_priority": "stop",
            "limit_fill_policy": "at_limit",
            "allow_exit_on_entry_bar": False,
            "allow_limit_stop_on_entry_bar": True,
            "allow_reentry_on_exit_bar": False,
            "close_request_timing": "current_close",
            "target_liquidity": "maker",
            "max_fill_fraction_per_bar": 1.0,
            "stop_limit_same_bar_policy": "defer",
        },
        fees={
            "maker_rate": MAKER_FEE,
            "taker_rate": MARKET_FEE,
        },
        risk={
            "default_risk_fraction": RISK_FRACTION,
            "max_risk_fraction": 0.05,
            "max_notional_multiple": MAX_NOTIONAL_MULTIPLE,
            "min_notional": 0.0,
            "min_quantity": 0.0,
            "quantity_step": 0.0,
            "min_stop_distance_pct": 0.0,
            "max_stop_distance_pct": None,
            "sizing_model": "historical_limit_cost",
        },
        output=OutputConfig(directory=output_root),
        seed=0,
        checkpoint=CheckpointConfig(mode="replay" if checkpoint else "disabled"),
        end_of_test_policy="force_close",
    )


def _load_journal(directory: Path) -> dict[str, Any]:
    value = json.loads((directory / "journal.json").read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("authoritative journal must be a JSON object")
    return cast(dict[str, Any], value)


def _load_final_equity(directory: Path) -> float:
    equity = pd.read_csv(directory / "equity.csv")
    if equity.empty or "equity" not in equity.columns:
        raise RuntimeError("authoritative equity artifact is missing the final equity")
    value = float(equity["equity"].iloc[-1])
    if not math.isfinite(value):
        raise RuntimeError("authoritative final equity must be finite")
    return value


def _exact_float(left: object, right: object, label: str) -> None:
    if isinstance(left, bool) or isinstance(right, bool):
        raise RuntimeError(f"{label}: boolean is not a numeric parity value")
    try:
        left_number = float(left)
        right_number = float(right)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{label}: parity value is not numeric") from exc
    if not math.isfinite(left_number) or not math.isfinite(right_number):
        raise RuntimeError(f"{label}: parity value must be finite")
    if left_number != right_number:
        raise RuntimeError(f"{label}: {left!r} != {right!r}")


def _mfe_r_from_frame(
    frame: pd.DataFrame,
    time_to_index: Mapping[str, int],
    *,
    signal: CanonicalSignalPlan,
    fill_time: str,
    exit_time: str,
    exit_reason: str,
) -> float:
    try:
        fill_index = time_to_index[fill_time]
        exit_index = time_to_index[exit_time]
    except KeyError as exc:
        raise RuntimeError("authoritative trade timestamp is outside the canonical frame") from exc
    if exit_index < fill_index:
        raise RuntimeError("authoritative exit precedes fill")
    mfe_price = signal.entry
    if exit_reason != "stop_on_fill_bar":
        for index in range(fill_index + 1, exit_index + 1):
            bar = frame.iloc[index]
            mfe_price = (
                max(mfe_price, float(bar["high"]))
                if signal.side == "long"
                else min(mfe_price, float(bar["low"]))
            )
    return _position_mfe_r(signal, mfe_price)


def _authoritative_ledger(
    journal: Mapping[str, Any],
    frame: pd.DataFrame,
    registered: RegisteredSignalPlan,
) -> pd.DataFrame:
    raw_trades = journal.get("trades")
    raw_fills = journal.get("fills")
    if not isinstance(raw_trades, list) or not isinstance(raw_fills, list):
        raise RuntimeError("authoritative journal lacks trade/fill arrays")
    fills_by_position: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for raw_fill in raw_fills:
        if not isinstance(raw_fill, Mapping):
            raise RuntimeError("authoritative fill is malformed")
        position_id = raw_fill.get("position_id")
        if not isinstance(position_id, str):
            raise RuntimeError("authoritative fill lacks position_id")
        fills_by_position[position_id].append(raw_fill)

    time_to_index = {
        timestamp.isoformat(): index for index, timestamp in enumerate(frame["open_time"])
    }
    rows: list[dict[str, object]] = []
    equity = INITIAL_EQUITY
    for trade_id, raw_trade in enumerate(raw_trades, start=1):
        if not isinstance(raw_trade, Mapping):
            raise RuntimeError("authoritative trade is malformed")
        tag = raw_trade.get("tag")
        position_id = raw_trade.get("position_id")
        if not isinstance(tag, str) or tag not in registered.by_key:
            raise RuntimeError("authoritative trade tag is outside canonical signals")
        if not isinstance(position_id, str):
            raise RuntimeError("authoritative trade lacks position_id")
        signal = registered.by_key[tag]
        fills = fills_by_position.get(position_id, [])
        if len(fills) != 2:
            raise RuntimeError("historical parity requires exactly one entry and one exit fill")
        entry_fills = [fill for fill in fills if fill.get("reason") == "entry"]
        exit_fills = [fill for fill in fills if fill.get("reason") != "entry"]
        if len(entry_fills) != 1 or len(exit_fills) != 1:
            raise RuntimeError("authoritative fill lifecycle differs from historical contract")
        entry_fill = entry_fills[0]
        exit_fill = exit_fills[0]
        fill_time = str(raw_trade["entry_time"])
        exit_time = str(raw_trade["exit_time"])
        core_reason = str(raw_trade["exit_reason"])
        exit_reason = "end_of_test" if core_reason == "end_of_data" else core_reason
        holding_bars = raw_trade.get("holding_bars")
        if not isinstance(holding_bars, int) or isinstance(holding_bars, bool):
            raise RuntimeError("authoritative trade lacks holding_bars")
        side = str(raw_trade["side"])
        expected_side = signal.side
        if side != expected_side:
            raise RuntimeError("authoritative trade side differs from canonical signal")
        initial_r = (
            signal.entry - signal.stop if signal.side == "long" else signal.stop - signal.entry
        )
        net_pnl = float(raw_trade["net_pnl"])
        equity_before = equity
        equity_after = equity_before + net_pnl
        row: dict[str, object] = {
            "trade_id": trade_id,
            "module_id": signal.module_id,
            "module_version": signal.module_version,
            "setup_id": signal.setup_id,
            "side": signal.side,
            "signal_time": signal.available_at.isoformat(),
            "fill_time": fill_time,
            "exit_time": exit_time,
            "entry_price": float(raw_trade["entry_price"]),
            "stop_price": signal.stop,
            "target_price": signal.target,
            "exit_price": float(raw_trade["exit_price"]),
            "quantity": float(raw_trade["quantity"]),
            "initial_r": initial_r,
            "target_r": signal.target_r,
            "exit_reason": exit_reason,
            "bars_held": holding_bars + 1,
            "gross_pnl": float(raw_trade["gross_pnl"]),
            "entry_fee": float(entry_fill["fee"]),
            "exit_fee": float(exit_fill["fee"]),
            "net_pnl": net_pnl,
            "equity_before": equity_before,
            "equity_after": equity_after,
            "return_on_equity": net_pnl / equity_before if equity_before else 0.0,
            "mfe_r": _mfe_r_from_frame(
                frame,
                time_to_index,
                signal=signal,
                fill_time=fill_time,
                exit_time=exit_time,
                exit_reason=exit_reason,
            ),
            "signal_key": signal.signal_key,
        }
        rows.append(row)
        equity = equity_after
    return pd.DataFrame(rows, columns=LEDGER_COLUMNS)


def _assert_ledger_exact(baseline: pd.DataFrame, authoritative: pd.DataFrame) -> None:
    if list(baseline.columns) != LEDGER_COLUMNS:
        raise RuntimeError("baseline ledger schema drift")
    if list(authoritative.columns) != LEDGER_COLUMNS:
        raise RuntimeError("authoritative parity ledger schema drift")
    if len(baseline) != len(authoritative):
        raise RuntimeError(
            f"trade count mismatch: baseline={len(baseline)} authoritative={len(authoritative)}"
        )
    numeric_columns = {
        "entry_price",
        "stop_price",
        "target_price",
        "exit_price",
        "quantity",
        "initial_r",
        "target_r",
        "bars_held",
        "gross_pnl",
        "entry_fee",
        "exit_fee",
        "net_pnl",
        "equity_before",
        "equity_after",
        "return_on_equity",
        "mfe_r",
        "trade_id",
    }
    for index in range(len(baseline)):
        for column in LEDGER_COLUMNS:
            left = baseline.iloc[index][column]
            right = authoritative.iloc[index][column]
            label = f"ledger[{index}].{column}"
            if column in numeric_columns:
                _exact_float(left, right, label)
            elif str(left) != str(right):
                raise RuntimeError(f"{label}: {left!r} != {right!r}")


def _comparison_metrics(ledger: pd.DataFrame) -> dict[str, float | int | None]:
    if ledger.empty:
        return {
            "final_equity": INITIAL_EQUITY,
            "max_closed_equity_drawdown": 0.0,
            "net_return": 0.0,
            "profit_factor": None,
            "trade_count": 0,
            "win_rate": 0.0,
        }
    final_equity = float(ledger["equity_after"].iloc[-1])
    gross_profit = float(ledger.loc[ledger["net_pnl"] > 0, "net_pnl"].sum())
    gross_loss = float(-ledger.loc[ledger["net_pnl"] < 0, "net_pnl"].sum())
    equity_curve = pd.Series(
        [INITIAL_EQUITY, *ledger["equity_after"].tolist()],
        dtype="float64",
    )
    drawdown = 1 - equity_curve / equity_curve.cummax()
    return {
        "final_equity": final_equity,
        "max_closed_equity_drawdown": float(drawdown.max()),
        "net_return": final_equity / INITIAL_EQUITY - 1,
        "profit_factor": gross_profit / gross_loss if gross_loss > 0 else None,
        "trade_count": len(ledger),
        "win_rate": float((ledger["net_pnl"] > 0).mean()),
    }


def _assert_metrics_exact(
    baseline_summary: Mapping[str, Any],
    authoritative_ledger: pd.DataFrame,
) -> dict[str, float | int | None]:
    raw_metrics = baseline_summary.get("metrics")
    if not isinstance(raw_metrics, Mapping):
        raise RuntimeError("baseline summary metrics are missing")
    actual = _comparison_metrics(authoritative_ledger)
    for key, value in actual.items():
        expected = raw_metrics.get(key)
        if value is None or expected is None:
            if value is not expected:
                raise RuntimeError(f"metric {key} mismatch: {expected!r} != {value!r}")
        elif isinstance(value, int):
            if expected != value:
                raise RuntimeError(f"metric {key} mismatch: {expected!r} != {value!r}")
        else:
            _exact_float(expected, value, f"metric.{key}")
    return actual


def _assert_reference_identity(summary: Mapping[str, Any]) -> None:
    if summary.get("candidate_id") != CANDIDATE_ID:
        raise RuntimeError("canonical candidate identity drift")
    if summary.get("composition_hash") != COMPOSITION_HASH:
        raise RuntimeError("canonical composition identity drift")
    if summary.get("semantic_digest") != REFERENCE_SEMANTIC_DIGEST:
        raise RuntimeError("canonical semantic digest drift")


def _assert_core_fill_contract(journal: Mapping[str, Any]) -> None:
    fills = journal.get("fills")
    trades = journal.get("trades")
    if not isinstance(fills, list) or not isinstance(trades, list):
        raise RuntimeError("authoritative journal fill/trade surface is missing")
    if len(fills) != len(trades) * 2:
        raise RuntimeError("historical parity requires exactly two fills per trade")
    for fill in fills:
        if not isinstance(fill, Mapping):
            raise RuntimeError("authoritative fill is malformed")
        reason = fill.get("reason")
        liquidity = fill.get("liquidity")
        if reason == "entry":
            if liquidity != "maker":
                raise RuntimeError("canonical entry fill must be maker")
        elif reason == "target":
            if liquidity != "maker":
                raise RuntimeError("canonical target fill must be maker")
        elif liquidity != "taker":
            raise RuntimeError("canonical non-target exit fill must be taker")


def _assert_order_contract(
    journal: Mapping[str, Any],
    registered: RegisteredSignalPlan,
) -> None:
    orders = journal.get("orders")
    if not isinstance(orders, list):
        raise RuntimeError("authoritative journal order surface is missing")
    submitted = [
        row for row in orders if isinstance(row, Mapping) and row.get("event") == "submitted"
    ]
    if not submitted:
        raise RuntimeError("authoritative parity emitted no orders")
    seen_ids: set[str] = set()
    for row in submitted:
        order = row.get("order")
        if not isinstance(order, Mapping):
            raise RuntimeError("submitted authoritative order is malformed")
        tag = order.get("tag")
        order_id = order.get("id")
        if not isinstance(tag, str) or tag not in registered.by_key:
            raise RuntimeError("submitted order tag is outside canonical signals")
        if not isinstance(order_id, str) or order_id in seen_ids:
            raise RuntimeError("submitted authoritative order id is invalid")
        seen_ids.add(order_id)
        signal = registered.by_key[tag]
        created_at = datetime.fromisoformat(str(order["created_at"]))
        if created_at != signal.available_at - _FIVE_MINUTES:
            raise RuntimeError("canonical pre-submit timestamp contract drift")
        expected_side = "buy" if signal.side == "long" else "sell"
        if order.get("side") != expected_side or order.get("order_type") != "limit":
            raise RuntimeError("canonical order side/type drift")
        _exact_float(signal.entry, order.get("price"), "order.entry_price")
        _exact_float(signal.stop, order.get("stop_loss"), "order.stop_loss")
        _exact_float(signal.target, order.get("take_profit"), "order.take_profit")


def _load_baseline(baseline_dir: Path) -> tuple[pd.DataFrame, dict[str, Any]]:
    ledger = pd.read_csv(baseline_dir / "combined-ledger.csv", float_precision="round_trip")
    summary = json.loads((baseline_dir / "summary.json").read_text(encoding="utf-8"))
    if not isinstance(summary, dict):
        raise RuntimeError("baseline summary must be a JSON object")
    return ledger, cast(dict[str, Any], summary)


def _run_one(
    *,
    input_path: Path,
    module_config: Path,
    output_root: Path,
    checkpoint: bool,
) -> tuple[dict[str, Any], str, bool, float]:
    outcome = run_backtest(
        _backtest_config(
            input_path=input_path,
            module_config=module_config,
            output_root=output_root,
            checkpoint=checkpoint,
        ),
        PerformanceConfig(mode="canonical_audit"),
        funding_source=None,
        valuation_policy="last",
        mark_price_source=None,
    )
    if outcome.bars_processed != 721_597:
        raise RuntimeError("authoritative Backtest processed unexpected bar count")
    journal = _load_journal(outcome.output_directory)
    final_equity = _load_final_equity(outcome.output_directory)
    return journal, outcome.run_id, outcome.restart_verified, final_equity


def run(
    input_path: Path,
    baseline_dir: Path,
    output_dir: Path,
    *,
    generated_from_commit: str,
    provenance_path: Path,
) -> dict[str, object]:
    provenance = _load_provenance(provenance_path, input_path)
    if provenance.get("generated_from_commit") != generated_from_commit:
        raise RuntimeError("dataset provenance commit mismatch")
    frame = load_5m(input_path, expected_sha256=str(provenance["parquet_sha256"]))
    if len(frame) != 721_597:
        raise RuntimeError("canonical historical corpus row-count drift")
    historical_signals = build_signals(frame)
    registered = register_signal_plan(historical_signals)
    baseline, baseline_summary = _load_baseline(baseline_dir)
    _assert_reference_identity(baseline_summary)
    output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="canonical-authoritative-parity-") as temp_name:
            temp_root = Path(temp_name)
            module_config = temp_root / "modules.yml"
            _write_module_config(module_config, registered.digest)

            journal, core_run_id, restart_verified, authoritative_final_equity = _run_one(
                input_path=input_path,
                module_config=module_config,
                output_root=temp_root / "authoritative",
                checkpoint=True,
            )
            if not restart_verified:
                raise RuntimeError("authoritative Backtest restart verification did not pass")

            _assert_core_fill_contract(journal)
            _assert_order_contract(journal, registered)
            authoritative = _authoritative_ledger(journal, frame, registered)
            _assert_ledger_exact(baseline, authoritative)
            metrics = _assert_metrics_exact(baseline_summary, authoritative)
            _exact_float(
                metrics["final_equity"],
                authoritative_final_equity,
                "authoritative_core.final_equity",
            )
            authoritative.to_csv(
                output_dir / "authoritative-comparison-ledger.csv",
                index=False,
                float_format="%.17g",
                lineterminator="\n",
            )
            report: dict[str, object] = {
                "schema_version": 1,
                "status": "AUTHORITATIVE_CORE_BACKTEST_HISTORICAL_PARITY",
                "generated_from_commit": generated_from_commit,
                "paper_only": True,
                "funding": "disabled",
                "valuation_policy": "last",
                "candidate_id": CANDIDATE_ID,
                "composition_hash": COMPOSITION_HASH,
                "reference_semantic_digest": REFERENCE_SEMANTIC_DIGEST,
                "signal_plan_digest": registered.digest,
                "signal_count": len(registered.signals),
                "trade_count": len(authoritative),
                "bars_processed": len(frame),
                "core_run_id": core_run_id,
                "authoritative_final_equity": authoritative_final_equity,
                "restart_verified": True,
                "ledger_exact": True,
                "metrics_exact": True,
                "metrics": metrics,
                "authoritative_boundary": (
                    "fills/trades/PnL/fees/sizing/slippage are emitted by modular_backtest/"
                    "modular_core; Research only maps canonical signals and compares outputs"
                ),
            }
            (output_dir / "parity-report.json").write_text(
                json.dumps(report, indent=2, sort_keys=True, allow_nan=False) + "\n",
                encoding="utf-8",
            )
            return report
    finally:
        unregister_signal_plan(registered.digest)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--generated-from-commit", required=True)
    parser.add_argument("--dataset-provenance", type=Path, required=True)
    arguments = parser.parse_args()
    report = run(
        arguments.input.resolve(strict=True),
        arguments.baseline.resolve(strict=True),
        arguments.output.resolve(),
        generated_from_commit=arguments.generated_from_commit,
        provenance_path=arguments.dataset_provenance.resolve(strict=True),
    )
    print(json.dumps(report, sort_keys=True, allow_nan=False))


if __name__ == "__main__":
    main()
