from __future__ import annotations

import csv
import json
import hashlib
import math
import platform
import sys
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any
from collections.abc import Mapping

from .events import EventEnvelope, EventStore, InMemoryEventStore
from .features import Feature
from .models import EquityPoint, Fill, OrderEvent, SetupEvent, Signal, Trade
from .replay import verify_event_stream
from .serialization import redact_sensitive_text
from .validation import finite_number, require_nonblank


@dataclass(slots=True)
class Journal:
    run_id: str = "run-uninitialized"
    event_store: EventStore = field(default_factory=InMemoryEventStore)
    setups: list[dict[str, Any]] = field(default_factory=list)
    setup_records: list[Any] = field(default_factory=list)
    features: list[dict[str, Any]] = field(default_factory=list)
    signals: list[dict[str, Any]] = field(default_factory=list)
    skips: list[dict[str, Any]] = field(default_factory=list)
    diagnostics: list[dict[str, Any]] = field(default_factory=list)
    orders: list[OrderEvent] = field(default_factory=list)
    fills: list[Fill] = field(default_factory=list)
    trades: list[Trade] = field(default_factory=list)
    equity: list[EquityPoint] = field(default_factory=list)
    errors: list[dict[str, Any]] = field(default_factory=list)
    _event_sequence: int = 0
    _last_event_by_entity: dict[str, str] = field(default_factory=dict)
    _event_ids: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        require_nonblank(self.run_id, "journal run_id")
        existing = self.event_store.read()
        if not existing:
            return
        report = verify_event_stream(existing)
        if not report.valid:
            raise ValueError(
                "cannot resume journal from invalid event stream: " + ",".join(report.errors)
            )
        if report.run_id != self.run_id:
            raise ValueError(
                f"journal run_id mismatch: expected {self.run_id}, got {report.run_id}"
            )
        self._event_sequence = report.last_sequence or 0
        self._event_ids = {event.event_id for event in existing}
        next_event_id = f"evt-{self._event_sequence + 1:010d}"
        if next_event_id in self._event_ids:
            raise ValueError(
                f"next deterministic event_id collides with durable stream: {next_event_id}"
            )
        for event in existing:
            if event.entity_key is not None:
                self._last_event_by_entity[event.entity_key] = event.event_id

    @property
    def events(self) -> tuple[EventEnvelope, ...]:
        return self.event_store.read()

    def emit_event(
        self,
        *,
        timestamp: datetime,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        symbol: str | None = None,
        entity_key: str | None = None,
    ) -> EventEnvelope:
        next_sequence = self._event_sequence + 1
        event_id = f"evt-{next_sequence:010d}"
        if event_id in self._event_ids:
            raise ValueError(f"duplicate deterministic event_id: {event_id}")
        event = EventEnvelope(
            event_id=event_id,
            run_id=self.run_id,
            sequence=next_sequence,
            timestamp=timestamp,
            event_type=event_type,
            source=source,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            symbol=symbol,
            entity_key=entity_key,
        )
        self.event_store.append(event)
        self._event_sequence = next_sequence
        self._event_ids.add(event.event_id)
        if entity_key is not None:
            self._last_event_by_entity[entity_key] = event.event_id
        return event

    def last_event_id(self, entity_key: str) -> str | None:
        require_nonblank(entity_key, "entity_key")
        return self._last_event_by_entity.get(entity_key)

    def _next_event(
        self,
        *,
        offset: int,
        timestamp: datetime,
        event_type: str,
        source: str,
        payload: dict[str, Any],
        correlation_id: str | None = None,
        causation_id: str | None = None,
        symbol: str | None = None,
        entity_key: str | None = None,
    ) -> EventEnvelope:
        sequence = self._event_sequence + offset
        event_id = f"evt-{sequence:010d}"
        if event_id in self._event_ids:
            raise ValueError(f"duplicate deterministic event_id: {event_id}")
        return EventEnvelope(
            event_id=event_id,
            run_id=self.run_id,
            sequence=sequence,
            timestamp=timestamp,
            event_type=event_type,
            source=source,
            payload=payload,
            correlation_id=correlation_id,
            causation_id=causation_id,
            symbol=symbol,
            entity_key=entity_key,
        )

    def _commit_event_batch(self, events: tuple[EventEnvelope, ...]) -> None:
        if not events:
            return
        self.event_store.append_many(events)
        self._event_sequence = events[-1].sequence
        for event in events:
            self._event_ids.add(event.event_id)
            if event.entity_key is not None:
                self._last_event_by_entity[event.entity_key] = event.event_id

    def log_setup(
        self, timestamp: datetime, setup: SetupEvent, *, symbol: str | None = None
    ) -> EventEnvelope:
        event = self.emit_event(
            timestamp=timestamp,
            event_type=f"setup.{setup.status.value}",
            source=setup.module,
            payload={"setup": to_primitive(setup)},
            correlation_id=setup.setup_id,
            causation_id=self.last_event_id(f"setup:{setup.setup_id}"),
            symbol=setup.symbol or symbol,
            entity_key=f"setup:{setup.setup_id}",
        )
        self.setups.append({"timestamp": timestamp, "setup": setup, "event_id": event.event_id})
        return event

    def log_feature(self, timestamp: datetime, feature: Feature, action: str) -> EventEnvelope:
        event = self.emit_event(
            timestamp=timestamp,
            event_type=f"feature.{action}",
            source=feature.source_module,
            payload={"feature": to_primitive(feature)},
            correlation_id=feature.feature_id,
            symbol=feature.symbol,
            entity_key=f"feature:{feature.feature_id}",
        )
        self.features.append(
            {
                "timestamp": timestamp,
                "action": action,
                "feature": feature,
                "event_id": event.event_id,
            }
        )
        return event

    def log_signal(
        self, timestamp: datetime, signal: Signal, *, symbol: str | None = None
    ) -> EventEnvelope:
        event = self.emit_event(
            timestamp=timestamp,
            event_type="signal.emitted",
            source=signal.module,
            payload={"signal": to_primitive(signal)},
            symbol=symbol,
        )
        self.signals.append({"timestamp": timestamp, "signal": signal, "event_id": event.event_id})
        return event

    def log_skip(
        self,
        timestamp: datetime,
        module: str,
        reason: str,
        *,
        symbol: str | None = None,
    ) -> None:
        reason_code, separator, reason_detail = reason.partition(":")
        row = {
            "timestamp": timestamp,
            "module": module,
            "reason": reason,
            "reason_code": reason_code,
            "reason_detail": reason_detail if separator else "",
        }
        self.emit_event(
            timestamp=timestamp,
            event_type="module.skip",
            source=module,
            payload={
                "reason": reason,
                "reason_code": reason_code,
                "reason_detail": row["reason_detail"],
            },
            symbol=symbol,
        )
        self.skips.append(row)

    def log_diagnostics(self, timestamp: datetime, module: str, values: dict[str, object]) -> None:
        if values:
            self.emit_event(
                timestamp=timestamp,
                event_type="diagnostic",
                source=module,
                payload={"values": to_primitive(values)},
            )
            self.diagnostics.append({"timestamp": timestamp, "module": module, "values": values})

    def record_order(
        self, event: OrderEvent, *, source: str | None = None, symbol: str | None = None
    ) -> OrderEvent:
        correlation = event.order.setup_id or event.order.id
        causation = (
            self.last_event_id(f"setup:{event.order.setup_id}")
            if event.order.setup_id
            else self.last_event_id(f"order:{event.order.id}")
        )
        envelope = self.emit_event(
            timestamp=event.timestamp,
            event_type=f"order.{event.event}",
            source=source or event.order.source_module,
            payload={"order": to_primitive(event.order), "reason": event.reason},
            correlation_id=correlation,
            causation_id=causation,
            symbol=symbol,
            entity_key=f"order:{event.order.id}",
        )
        enriched = OrderEvent(
            timestamp=event.timestamp,
            event=event.event,
            order=event.order,
            reason=event.reason,
            event_id=envelope.event_id,
            correlation_id=correlation,
            causation_id=causation,
        )
        self.orders.append(enriched)
        return enriched

    def record_fill(
        self, fill: Fill, *, source: str = "execution", symbol: str | None = None
    ) -> EventEnvelope:
        event = self.emit_event(
            timestamp=fill.timestamp,
            event_type="fill.recorded",
            source=source,
            payload={"fill": to_primitive(fill)},
            correlation_id=fill.position_id or fill.order_id,
            causation_id=self.last_event_id(f"order:{fill.order_id}"),
            symbol=symbol,
            entity_key=f"fill:{fill.fill_id or len(self.fills) + 1}",
        )
        self.fills.append(fill)
        return event

    def record_trade(
        self, trade: Trade, *, source: str = "portfolio", symbol: str | None = None
    ) -> EventEnvelope:
        event = self.emit_event(
            timestamp=trade.exit_time,
            event_type="position.closed",
            source=source,
            payload={"trade": to_primitive(trade)},
            correlation_id=trade.setup_id or trade.position_id,
            causation_id=(
                self.last_event_id(f"setup:{trade.setup_id}") if trade.setup_id else None
            ),
            symbol=symbol,
            entity_key=f"position:{trade.position_id}",
        )
        self.trades.append(trade)
        return event

    def record_fill_and_trade(
        self,
        fill: Fill,
        trade: Trade,
        *,
        symbol: str | None = None,
    ) -> tuple[EventEnvelope, EventEnvelope]:
        fill_event = self._next_event(
            offset=1,
            timestamp=fill.timestamp,
            event_type="fill.recorded",
            source="execution",
            payload={"fill": to_primitive(fill)},
            correlation_id=fill.position_id or fill.order_id,
            causation_id=self.last_event_id(f"order:{fill.order_id}"),
            symbol=symbol,
            entity_key=f"fill:{fill.fill_id or len(self.fills) + 1}",
        )
        trade_event = self._next_event(
            offset=2,
            timestamp=trade.exit_time,
            event_type="position.closed",
            source="portfolio",
            payload={"trade": to_primitive(trade)},
            correlation_id=trade.setup_id or trade.position_id,
            causation_id=fill_event.event_id,
            symbol=symbol,
            entity_key=f"position:{trade.position_id}",
        )
        self._commit_event_batch((fill_event, trade_event))
        self.fills.append(fill)
        self.trades.append(trade)
        return fill_event, trade_event

    def record_equity(self, point: EquityPoint, *, symbol: str | None = None) -> None:
        self.emit_event(
            timestamp=point.timestamp,
            event_type="portfolio.mark",
            source="portfolio",
            payload={"equity": to_primitive(point)},
            symbol=symbol,
        )
        self.equity.append(point)

    def log_error(self, timestamp: datetime, stage: str, error: Exception) -> None:
        error_repr = redact_sensitive_text(repr(error))
        error_message = redact_sensitive_text(str(error))
        row = {
            "timestamp": timestamp,
            "stage": stage,
            "error": error_repr,
            "error_type": type(error).__name__,
            "error_message": error_message,
        }
        self.emit_event(
            timestamp=timestamp,
            event_type="kernel.error",
            source=stage,
            payload={
                "error": error_repr,
                "error_type": row["error_type"],
                "error_message": error_message,
            },
        )
        self.errors.append(row)

    def export_directory(self, path: str | Path) -> Path:
        output = Path(path)
        output.mkdir(parents=True, exist_ok=True)
        payload = {
            "setups": self.setups,
            "setup_records": self.setup_records,
            "features": self.features,
            "signals": self.signals,
            "skips": self.skips,
            "diagnostics": self.diagnostics,
            "orders": self.orders,
            "fills": self.fills,
            "trades": self.trades,
            "equity": self.equity,
            "errors": self.errors,
            "events": self.events,
        }
        (output / "journal.json").write_text(
            json.dumps(to_primitive(payload), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        with (output / "events.jsonl").open("w", encoding="utf-8") as handle:
            for event in self.events:
                handle.write(json.dumps(to_primitive(event), ensure_ascii=False) + "\n")
        self._write_csv(output / "trades.csv", [to_primitive(x) for x in self.trades])
        self._write_csv(output / "equity.csv", [to_primitive(x) for x in self.equity])
        environment = {
            "python": sys.version,
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        }
        (output / "environment.json").write_text(
            json.dumps(environment, ensure_ascii=False, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        checksums: list[str] = []
        for artifact in sorted(
            path for path in output.iterdir() if path.is_file() and path.name != "SHA256SUMS.txt"
        ):
            digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
            checksums.append(f"{digest}  {artifact.name}")
        (output / "SHA256SUMS.txt").write_text("\n".join(checksums) + "\n", encoding="utf-8")
        return output

    @staticmethod
    def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
        if not rows:
            path.write_text("", encoding="utf-8")
            return
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)


@dataclass(frozen=True, slots=True)
class Metrics:
    initial_equity: float
    final_equity: float
    net_profit: float
    return_pct: float
    trades: int
    wins: int
    losses: int
    breakevens: int
    win_rate: float
    gross_profit: float
    gross_loss: float
    profit_factor: float
    expectancy: float
    average_win: float
    average_loss: float
    payoff_ratio: float
    max_drawdown: float
    max_drawdown_pct: float
    max_consecutive_wins: int
    max_consecutive_losses: int
    average_r_multiple: float | None
    average_holding_bars: float | None
    exposure_pct: float
    total_fees: float

    def __post_init__(self) -> None:
        _validate_metrics(self)


def _validate_metrics(metrics: Metrics) -> None:
    initial_equity = finite_number(metrics.initial_equity, "initial_equity")
    final_equity = finite_number(metrics.final_equity, "final_equity")
    net_profit = finite_number(metrics.net_profit, "net_profit")
    return_pct = finite_number(metrics.return_pct, "return_pct")
    win_rate = finite_number(metrics.win_rate, "win_rate")
    gross_profit = finite_number(metrics.gross_profit, "gross_profit")
    gross_loss = finite_number(metrics.gross_loss, "gross_loss")
    profit_factor = _nonnegative_extended_number(metrics.profit_factor, "profit_factor")
    expectancy = finite_number(metrics.expectancy, "expectancy")
    average_win = finite_number(metrics.average_win, "average_win")
    average_loss = finite_number(metrics.average_loss, "average_loss")
    payoff_ratio = _nonnegative_extended_number(metrics.payoff_ratio, "payoff_ratio")
    max_drawdown = finite_number(metrics.max_drawdown, "max_drawdown")
    max_drawdown_pct = finite_number(metrics.max_drawdown_pct, "max_drawdown_pct")
    exposure_pct = finite_number(metrics.exposure_pct, "exposure_pct")
    total_fees = finite_number(metrics.total_fees, "total_fees")

    trades = _nonnegative_int(metrics.trades, "trades")
    wins = _nonnegative_int(metrics.wins, "wins")
    losses = _nonnegative_int(metrics.losses, "losses")
    breakevens = _nonnegative_int(metrics.breakevens, "breakevens")
    max_consecutive_wins = _nonnegative_int(metrics.max_consecutive_wins, "max_consecutive_wins")
    max_consecutive_losses = _nonnegative_int(
        metrics.max_consecutive_losses, "max_consecutive_losses"
    )

    if wins + losses + breakevens != trades:
        raise ValueError("wins + losses + breakevens must equal trades")
    _validate_streak(max_consecutive_wins, wins, "max_consecutive_wins", "wins")
    _validate_streak(max_consecutive_losses, losses, "max_consecutive_losses", "losses")

    for name, value in (
        ("gross_profit", gross_profit),
        ("gross_loss", gross_loss),
        ("max_drawdown", max_drawdown),
        ("max_drawdown_pct", max_drawdown_pct),
        ("total_fees", total_fees),
    ):
        if value < 0:
            raise ValueError(f"{name} cannot be negative")
    if average_win < 0:
        raise ValueError("average_win cannot be negative")
    if average_loss > 0:
        raise ValueError("average_loss cannot be positive")
    if not 0 <= win_rate <= 100:
        raise ValueError("win_rate must be in [0, 100]")
    if not 0 <= exposure_pct <= 100:
        raise ValueError("exposure_pct must be in [0, 100]")

    if metrics.average_r_multiple is not None:
        finite_number(metrics.average_r_multiple, "average_r_multiple")
    if metrics.average_holding_bars is not None:
        average_holding_bars = finite_number(metrics.average_holding_bars, "average_holding_bars")
        if average_holding_bars < 0:
            raise ValueError("average_holding_bars cannot be negative")

    _require_metric_close(net_profit, final_equity - initial_equity, "net_profit identity")
    expected_return = net_profit / initial_equity * 100 if initial_equity else 0.0
    _require_metric_close(return_pct, expected_return, "return_pct identity")
    expected_win_rate = wins / trades * 100 if trades else 0.0
    _require_metric_close(win_rate, expected_win_rate, "win_rate identity")

    expected_gross_profit = average_win * wins
    expected_gross_loss = abs(average_loss) * losses
    _require_metric_close(gross_profit, expected_gross_profit, "gross_profit identity")
    _require_metric_close(gross_loss, expected_gross_loss, "gross_loss identity")

    expected_expectancy = (gross_profit - gross_loss) / trades if trades else 0.0
    _require_metric_close(expectancy, expected_expectancy, "expectancy identity")

    expected_profit_factor = (
        gross_profit / gross_loss if gross_loss > 0 else (float("inf") if gross_profit > 0 else 0.0)
    )
    _require_metric_close(profit_factor, expected_profit_factor, "profit_factor identity")

    expected_payoff_ratio = (
        average_win / abs(average_loss) if average_loss else (float("inf") if average_win else 0.0)
    )
    _require_metric_close(payoff_ratio, expected_payoff_ratio, "payoff_ratio identity")


def _nonnegative_int(value: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be int")
    if value < 0:
        raise ValueError(f"{name} cannot be negative")
    return value


def _nonnegative_extended_number(value: float, name: str) -> float:
    try:
        number = finite_number(value, name)
    except ValueError:
        number = float(value)
        if number != float("inf"):
            raise
    if number < 0:
        raise ValueError(f"{name} cannot be negative")
    return number


def _validate_streak(streak: int, count: int, streak_name: str, count_name: str) -> None:
    if count == 0:
        if streak != 0:
            raise ValueError(f"{streak_name} must be zero when {count_name} is zero")
        return
    if not 1 <= streak <= count:
        raise ValueError(f"{streak_name} must be in [1, {count_name}]")


def _require_metric_close(actual: float, expected: float, name: str) -> None:
    if actual == expected:
        return
    if not math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError(f"{name} is inconsistent: expected {expected}, got {actual}")


def calculate_metrics(journal: Journal, initial_equity: float) -> Metrics:
    trades = journal.trades
    pnls = [trade.net_pnl for trade in trades]
    winning = [p for p in pnls if p > 0]
    losing = [p for p in pnls if p < 0]
    gross_profit = sum(winning)
    gross_loss_abs = abs(sum(losing))
    wins = len(winning)
    losses = len(losing)
    breakevens = len(pnls) - wins - losses
    final_equity = journal.equity[-1].equity if journal.equity else initial_equity
    net_profit = final_equity - initial_equity
    profit_factor = (
        gross_profit / gross_loss_abs
        if gross_loss_abs > 0
        else (float("inf") if gross_profit > 0 else 0.0)
    )
    expectancy = sum(pnls) / len(pnls) if pnls else 0.0
    average_win = gross_profit / wins if wins else 0.0
    average_loss = -gross_loss_abs / losses if losses else 0.0
    payoff_ratio = (
        average_win / abs(average_loss) if average_loss else (float("inf") if average_win else 0.0)
    )

    peak = initial_equity
    max_dd = 0.0
    max_dd_pct = 0.0
    for point in journal.equity:
        peak = max(peak, point.equity)
        drawdown = peak - point.equity
        max_dd = max(max_dd, drawdown)
        if peak > 0:
            max_dd_pct = max(max_dd_pct, drawdown / peak)

    max_wins, max_losses = _consecutive_streaks(pnls)
    r_values = [trade.r_multiple for trade in trades if trade.r_multiple is not None]
    holding_values = [trade.holding_bars for trade in trades if trade.holding_bars is not None]
    exposure_points = sum(1 for point in journal.equity if point.position_open)

    return Metrics(
        initial_equity=initial_equity,
        final_equity=final_equity,
        net_profit=net_profit,
        return_pct=(net_profit / initial_equity * 100) if initial_equity else 0.0,
        trades=len(trades),
        wins=wins,
        losses=losses,
        breakevens=breakevens,
        win_rate=(wins / len(trades) * 100) if trades else 0.0,
        gross_profit=gross_profit,
        gross_loss=gross_loss_abs,
        profit_factor=profit_factor,
        expectancy=expectancy,
        average_win=average_win,
        average_loss=average_loss,
        payoff_ratio=payoff_ratio,
        max_drawdown=max_dd,
        max_drawdown_pct=max_dd_pct * 100,
        max_consecutive_wins=max_wins,
        max_consecutive_losses=max_losses,
        average_r_multiple=sum(r_values) / len(r_values) if r_values else None,
        average_holding_bars=sum(holding_values) / len(holding_values) if holding_values else None,
        exposure_pct=exposure_points / len(journal.equity) * 100 if journal.equity else 0.0,
        total_fees=sum(trade.fees for trade in trades),
    )


def _consecutive_streaks(pnls: list[float]) -> tuple[int, int]:
    best_wins = best_losses = current_wins = current_losses = 0
    for pnl in pnls:
        if pnl > 0:
            current_wins += 1
            current_losses = 0
        elif pnl < 0:
            current_losses += 1
            current_wins = 0
        else:
            current_wins = current_losses = 0
        best_wins = max(best_wins, current_wins)
        best_losses = max(best_losses, current_losses)
    return best_wins, best_losses


def to_primitive(value: Any) -> Any:
    if is_dataclass(value):
        return {item.name: to_primitive(getattr(value, item.name)) for item in fields(value)}
    if isinstance(value, Mapping):
        return {str(key): to_primitive(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [to_primitive(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if value == float("inf"):
            return "inf"
        if value == float("-inf"):
            return "-inf"
        if value != value:
            return "nan"
    return value
