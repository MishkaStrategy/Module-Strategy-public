"""Single-order single-position deterministic portfolio execution."""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

from ._common import (
    ADVERSE_SLIPPAGE,
    CANDIDATE_ID,
    COMPOSITION_HASH,
    INITIAL_EQUITY,
    LEDGER_COLUMNS,
    MAKER_FEE,
    MARKET_FEE,
    MAX_NOTIONAL_MULTIPLE,
    REGISTERED_DATASET_HASH,
    RISK_FRACTION,
    PendingOrder,
    Position,
    Side,
    Signal,
    canonical_sha256,
    sha256_file,
)

SEMANTIC_DIGEST_CONTRACT = "canonical-strategy-v2-result-v1"
_REGISTERED_CANONICAL_SOURCE = "REGISTERED_CANONICAL_BINANCE_USDTM_CORPUS"
_REGISTERED_CANONICAL_RELATION = "PROVEN_BY_REGISTERED_CANONICAL_IDENTITY"
_OFFICIAL_REST_SOURCE = "OFFICIAL_BINANCE_USDTM_REST_API"
_OFFICIAL_REST_CONTENT_SHA256 = "7caf6cedc99e77991a1b618ee77c3b31f5433342ba88122fa6a1614708fed130"


def _stop_exit_price(side: Side, stop: float, bar_open: float) -> float:
    if side == "long":
        base = min(stop, bar_open) if bar_open < stop else stop
        return base * (1 - ADVERSE_SLIPPAGE)
    base = max(stop, bar_open) if bar_open > stop else stop
    return base * (1 + ADVERSE_SLIPPAGE)


def _market_exit_price(side: Side, price: float) -> float:
    return price * (1 - ADVERSE_SLIPPAGE) if side == "long" else price * (1 + ADVERSE_SLIPPAGE)


def _quantity(equity: float, signal: Signal) -> tuple[float, float, float]:
    stop_fill = _stop_exit_price(signal.side, signal.stop, signal.stop)
    price_loss = signal.entry - stop_fill if signal.side == "long" else stop_fill - signal.entry
    per_unit_risk = price_loss + signal.entry * MAKER_FEE + stop_fill * MARKET_FEE
    if per_unit_risk <= 0:
        return 0.0, stop_fill, 0.0
    quantity = equity * RISK_FRACTION / per_unit_risk
    quantity = min(quantity, equity * MAX_NOTIONAL_MULTIPLE / signal.entry)
    return quantity, stop_fill, per_unit_risk


def _close_position(
    position: Position,
    *,
    exit_index: int,
    exit_time: pd.Timestamp,
    exit_price: float,
    exit_fee_rate: float,
    exit_reason: str,
    equity: float,
    trade_id: int,
) -> tuple[dict[str, object], float]:
    signal = position.signal
    gross = (
        (exit_price - signal.entry) * position.quantity
        if signal.side == "long"
        else (signal.entry - exit_price) * position.quantity
    )
    exit_fee = exit_price * position.quantity * exit_fee_rate
    net = gross - position.entry_fee - exit_fee
    equity_after = equity + net
    mfe = (
        (position.mfe_price - signal.entry) / position.initial_r
        if signal.side == "long"
        else (signal.entry - position.mfe_price) / position.initial_r
    )
    row: dict[str, object] = {
        "trade_id": trade_id,
        "module_id": signal.module_id,
        "module_version": signal.module_version,
        "setup_id": signal.setup_id,
        "side": signal.side,
        "signal_time": signal.available_at.isoformat(),
        "fill_time": position.fill_time.isoformat(),
        "exit_time": exit_time.isoformat(),
        "entry_price": signal.entry,
        "stop_price": signal.stop,
        "target_price": position.target,
        "exit_price": exit_price,
        "quantity": position.quantity,
        "initial_r": position.initial_r,
        "target_r": signal.target_r,
        "exit_reason": exit_reason,
        "bars_held": exit_index - position.fill_index + 1,
        "gross_pnl": gross,
        "entry_fee": position.entry_fee,
        "exit_fee": exit_fee,
        "net_pnl": net,
        "equity_before": equity,
        "equity_after": equity_after,
        "return_on_equity": net / equity if equity else 0.0,
        "mfe_r": max(0.0, mfe),
        "signal_key": signal.signal_key,
    }
    return row, equity_after


def _select_signal(incoming: list[Signal]) -> Signal:
    return min(
        incoming,
        key=lambda signal: (
            signal.priority,
            {"long": 0, "short_a": 1, "short_b": 2, "short": 0}[signal.setup_id],
            signal.module_id,
            signal.signal_key,
        ),
    )


def run_portfolio(
    frame: pd.DataFrame, signals: list[Signal]
) -> tuple[pd.DataFrame, dict[str, int]]:
    time_to_index = {timestamp: index for index, timestamp in enumerate(frame["open_time"])}
    by_index: dict[int, list[Signal]] = {}
    counts: dict[str, int] = {
        "fvg_rt_final_15m": 0,
        "frozen_1h_short": 0,
        "frozen_4h_short": 0,
    }
    for signal in signals:
        counts[signal.module_id] += 1
        index = time_to_index.get(signal.available_at)
        if index is not None:
            by_index.setdefault(index, []).append(signal)
    equity = INITIAL_EQUITY
    pending: PendingOrder | None = None
    position: Position | None = None
    ledger: list[dict[str, object]] = []
    trade_id = 1
    for index, bar in frame.iterrows():
        timestamp = bar["open_time"]
        if position is None and pending is not None and index >= pending.expires_before_index:
            pending = None
        incoming = by_index.get(index, [])
        if position is None and incoming:
            selected = _select_signal(incoming)
            if pending is None or selected.priority < pending.signal.priority:
                pending = PendingOrder(
                    signal=selected,
                    created_index=index,
                    expires_before_index=index + selected.ttl_bars,
                )
        if position is None and pending is not None:
            signal = pending.signal
            if signal.side == "long":
                touched = bar["low"] <= signal.entry
            else:
                touched = bar["high"] >= signal.entry
            if touched:
                quantity, _, _ = _quantity(equity, signal)
                if quantity > 0:
                    initial_r = (
                        signal.entry - signal.stop
                        if signal.side == "long"
                        else signal.stop - signal.entry
                    )
                    target = (
                        signal.entry + signal.target_r * initial_r
                        if signal.side == "long"
                        else signal.entry - signal.target_r * initial_r
                    )
                    position = Position(
                        signal=signal,
                        fill_index=index,
                        fill_time=timestamp,
                        quantity=quantity,
                        entry_fee=signal.entry * quantity * MAKER_FEE,
                        initial_r=initial_r,
                        target=target,
                        mfe_price=signal.entry,
                    )
                    pending = None
                    stop_hit = (
                        bar["low"] <= signal.stop
                        if signal.side == "long"
                        else bar["high"] >= signal.stop
                    )
                    if stop_hit:
                        exit_price = _stop_exit_price(signal.side, signal.stop, bar["open"])
                        row, equity = _close_position(
                            position,
                            exit_index=index,
                            exit_time=timestamp,
                            exit_price=exit_price,
                            exit_fee_rate=MARKET_FEE,
                            exit_reason="stop_on_fill_bar",
                            equity=equity,
                            trade_id=trade_id,
                        )
                        ledger.append(row)
                        trade_id += 1
                        position = None
        if position is None:
            continue
        signal = position.signal
        if index == position.fill_index:
            continue
        if signal.side == "long":
            position.mfe_price = max(position.mfe_price, float(bar["high"]))
            stop_hit = bar["low"] <= signal.stop
            target_hit = bar["high"] >= position.target
        else:
            position.mfe_price = min(position.mfe_price, float(bar["low"]))
            stop_hit = bar["high"] >= signal.stop
            target_hit = bar["low"] <= position.target
        exit_reason: str | None = None
        exit_price = 0.0
        exit_fee = MARKET_FEE
        if stop_hit:
            exit_reason = "stop"
            exit_price = _stop_exit_price(signal.side, signal.stop, bar["open"])
        elif target_hit:
            exit_reason = "target"
            exit_price = position.target
            exit_fee = MAKER_FEE
        else:
            bars_since_fill = index - position.fill_index
            current_mfe_r = (
                (position.mfe_price - signal.entry) / position.initial_r
                if signal.side == "long"
                else (signal.entry - position.mfe_price) / position.initial_r
            )
            if (
                bars_since_fill == signal.no_progress_bars
                and current_mfe_r < signal.no_progress_min_r
            ):
                exit_reason = "no_progress"
                exit_price = _market_exit_price(signal.side, float(bar["close"]))
            elif bars_since_fill >= signal.max_hold_bars - 1:
                exit_reason = "max_hold"
                exit_price = _market_exit_price(signal.side, float(bar["close"]))
        if exit_reason is not None:
            row, equity = _close_position(
                position,
                exit_index=index,
                exit_time=timestamp,
                exit_price=exit_price,
                exit_fee_rate=exit_fee,
                exit_reason=exit_reason,
                equity=equity,
                trade_id=trade_id,
            )
            ledger.append(row)
            trade_id += 1
            position = None
    if position is not None:
        last = frame.iloc[-1]
        exit_price = _market_exit_price(position.signal.side, float(last["close"]))
        row, equity = _close_position(
            position,
            exit_index=len(frame) - 1,
            exit_time=last["open_time"],
            exit_price=exit_price,
            exit_fee_rate=MARKET_FEE,
            exit_reason="end_of_test",
            equity=equity,
            trade_id=trade_id,
        )
        ledger.append(row)
    return pd.DataFrame(ledger, columns=LEDGER_COLUMNS), counts


def _write_csv(frame: pd.DataFrame, path: Path) -> str:
    frame.to_csv(path, index=False, float_format="%.17g", lineterminator="\n")
    return sha256_file(path)


def _semantic_digest(summary: dict[str, object]) -> str:
    payload = {
        key: value
        for key, value in summary.items()
        if key not in {"generated_from_commit", "semantic_digest"}
    }
    dataset = payload.get("dataset")
    if isinstance(dataset, dict):
        semantic_dataset = dict(dataset)
        if (
            semantic_dataset.get("input_kind") == _REGISTERED_CANONICAL_SOURCE
            and semantic_dataset.get("registered_dataset_relation")
            == _REGISTERED_CANONICAL_RELATION
            and semantic_dataset.get("content_sha256") == _OFFICIAL_REST_CONTENT_SHA256
            and semantic_dataset.get("retained_registered_dataset_hash") == REGISTERED_DATASET_HASH
            and semantic_dataset.get("drive_comparison_status") == "NOT_REQUESTED"
            and semantic_dataset.get("drive_content_parity") is None
        ):
            semantic_dataset["input_kind"] = _OFFICIAL_REST_SOURCE
            semantic_dataset["registered_dataset_relation"] = "UNPROVEN_FOR_THIS_RUN"
        payload["dataset"] = semantic_dataset
    return canonical_sha256(payload)


def summarize(
    ledger: pd.DataFrame,
    *,
    signal_counts: dict[str, int],
    output_dir: Path,
    input_path: Path,
    row_count: int,
    generated_from_commit: str,
    dataset_provenance: dict[str, object],
) -> dict[str, object]:
    output_dir.mkdir(parents=True, exist_ok=True)
    ledger_hash = _write_csv(ledger, output_dir / "combined-ledger.csv")
    component_ledgers: dict[str, dict[str, object]] = {}
    for module_id in signal_counts:
        component = ledger.loc[ledger["module_id"].eq(module_id)].copy()
        path = output_dir / f"{module_id}-ledger.csv"
        component_ledgers[module_id] = {
            "path": path.name,
            "sha256": _write_csv(component, path),
            "trade_count": len(component),
        }
    final_equity = float(ledger["equity_after"].iloc[-1]) if not ledger.empty else INITIAL_EQUITY
    gross_profit = float(ledger.loc[ledger["net_pnl"] > 0, "net_pnl"].sum())
    gross_loss = float(-ledger.loc[ledger["net_pnl"] < 0, "net_pnl"].sum())
    profit_factor = gross_profit / gross_loss if gross_loss > 0 else None
    equity_curve = pd.Series([INITIAL_EQUITY, *ledger["equity_after"].tolist()], dtype="float64")
    drawdown = 1 - equity_curve / equity_curve.cummax()
    comparison_status = str(dataset_provenance["drive_comparison_status"])
    source = str(dataset_provenance["source"])
    if source == _REGISTERED_CANONICAL_SOURCE:
        registered_relation = _REGISTERED_CANONICAL_RELATION
    elif comparison_status == "MATCH":
        registered_relation = "MATCHED_VIA_DRIVE_PARITY"
    else:
        registered_relation = "UNPROVEN_FOR_THIS_RUN"
    summary: dict[str, object] = {
        "candidate_id": CANDIDATE_ID,
        "composition_hash": COMPOSITION_HASH,
        "dataset": {
            "content_sha256": dataset_provenance["content_sha256"],
            "drive_comparison_status": comparison_status,
            "drive_content_parity": dataset_provenance["drive_content_parity"],
            "endpoint": dataset_provenance["endpoint"],
            "input_kind": source,
            "raw_5m_sha256": sha256_file(input_path),
            "registered_dataset_relation": registered_relation,
            "retained_registered_dataset_hash": REGISTERED_DATASET_HASH,
            "retained_drive_raw_sha256": dataset_provenance["retained_drive_raw_sha256"],
            "row_count": row_count,
            "symbol": "BTCUSDT",
            "timeframe": "5m",
            "timezone": "UTC",
        },
        "fees": {
            "adverse_slippage": ADVERSE_SLIPPAGE,
            "maker": MAKER_FEE,
            "market_and_stop": MARKET_FEE,
        },
        "generated_from_commit": generated_from_commit,
        "historical_reference_counts_are_mandatory": False,
        "ledgers": {
            "combined": {
                "path": "combined-ledger.csv",
                "sha256": ledger_hash,
                "trade_count": len(ledger),
            },
            "components": component_ledgers,
        },
        "metrics": {
            "final_equity": final_equity,
            "max_closed_equity_drawdown": float(drawdown.max()),
            "net_return": final_equity / INITIAL_EQUITY - 1,
            "profit_factor": profit_factor,
            "trade_count": len(ledger),
            "win_rate": float((ledger["net_pnl"] > 0).mean()) if not ledger.empty else 0.0,
        },
        "risk": {
            "initial_equity": INITIAL_EQUITY,
            "max_notional_multiple": MAX_NOTIONAL_MULTIPLE,
            "risk_fraction": RISK_FRACTION,
        },
        "schema_version": 2,
        "semantic_digest_contract": SEMANTIC_DIGEST_CONTRACT,
        "signal_counts_before_portfolio": signal_counts,
        "status": "OFFICIAL_ENDPOINT_BACKTEST_COMPLETE",
    }
    summary["semantic_digest"] = _semantic_digest(summary)
    summary_path = output_dir / "summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return summary
