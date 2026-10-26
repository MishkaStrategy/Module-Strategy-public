from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Protocol

from .domain import (
    CandleBatch,
    InstrumentRules,
    SignalStatus,
    StrategyContext,
    StrategyDecision,
    TradeSignal,
)


class SignalStrategy(Protocol):
    @property
    def strategy_id(self) -> str: ...
    @property
    def version(self) -> str: ...
    @property
    def required_timeframes(self) -> tuple[str, ...]: ...
    def warmup_bars(self, timeframe: str) -> int: ...
    def evaluate(self, context: StrategyContext) -> StrategyDecision: ...


class MarketDataProvider(Protocol):
    def get_closed_candles(
        self,
        instrument: InstrumentRules,
        timeframe: str,
        limit: int,
        *,
        as_of: datetime,
    ) -> CandleBatch: ...


class StateStore(Protocol):
    def register(self, signal: TradeSignal) -> bool: ...
    def transition(
        self,
        signal_id: str,
        status: SignalStatus,
        *,
        error: str | None = None,
        retry_at: datetime | None = None,
        message_id: str | None = None,
    ) -> None: ...
    def status(self, signal_id: str) -> SignalStatus | None: ...
    def get_signal(self, signal_id: str) -> TradeSignal | None: ...
    def pending(self, *, now: datetime, symbol: str | None = None) -> tuple[TradeSignal, ...]: ...
    def last_processed(self, symbol: str, timeframe: str) -> datetime | None: ...
    def set_last_processed(self, symbol: str, timeframe: str, timestamp: datetime) -> None: ...


class ChartRenderer(Protocol):
    def render(self, signal: TradeSignal, batch: CandleBatch) -> Path: ...


class Notifier(Protocol):
    def send(self, signal: TradeSignal, message: str, chart: Path | None) -> str: ...


class LifecycleNotifier(Protocol):
    def send_lifecycle(self, signal: TradeSignal, status: SignalStatus, message: str) -> str: ...


class Clock(Protocol):
    def now(self) -> datetime: ...
