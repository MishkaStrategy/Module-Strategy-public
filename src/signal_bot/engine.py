from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import (
    ChartRenderer,
    Clock,
    LifecycleNotifier,
    MarketDataProvider,
    Notifier,
    SignalStrategy,
    StateStore,
)
from .domain import (
    CandleBatch,
    InstrumentRules,
    SignalStatus,
    StrategyContext,
    TradeSignal,
)
from .formatter import format_lifecycle, format_signal
from .validation import SignalValidationError, validate_decision, validate_signal

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class SignalEngine:
    def __init__(
        self,
        *,
        market_data: MarketDataProvider,
        state: StateStore,
        renderer: ChartRenderer,
        notifier: Notifier,
        clock: Clock | None = None,
        max_send_attempts: int = 3,
        lifecycle_notifier: LifecycleNotifier | None = None,
    ) -> None:
        if (
            isinstance(max_send_attempts, bool)
            or not isinstance(max_send_attempts, int)
            or max_send_attempts <= 0
        ):
            raise ValueError("max_send_attempts must be a positive integer")
        self.market_data, self.state = market_data, state
        self.renderer, self.notifier = renderer, notifier
        self.clock, self.max_send_attempts = clock or SystemClock(), max_send_attempts
        self.lifecycle_notifier = lifecycle_notifier

    def process(
        self,
        instrument: InstrumentRules,
        strategy: SignalStrategy,
        *,
        chart_enabled: bool = True,
    ) -> TradeSignal | None:
        now = self.clock.now().astimezone(timezone.utc)
        if not strategy.required_timeframes:
            raise ValueError("strategy must require at least one timeframe")
        batches: dict[str, CandleBatch] = {}
        advanced = False
        for timeframe in strategy.required_timeframes:
            limit = strategy.warmup_bars(timeframe) + 2
            batch = self.market_data.get_closed_candles(instrument, timeframe, limit, as_of=now)
            batches[timeframe] = batch
            latest = batch.candles[-1].timestamp
            cursor = self.state.last_processed(instrument.symbol, timeframe)
            if cursor is None or latest > cursor:
                advanced = True
        if not advanced:
            return None

        decision = strategy.evaluate(StrategyContext(instrument, batches, now))
        if not decision.has_signal:
            self._advance_cursors(instrument.symbol, batches)
            return None
        signal_timeframe = decision.signal_timeframe
        if signal_timeframe is None or signal_timeframe not in batches:
            raise SignalValidationError(
                "signal timeframe must be one of the strategy required timeframes"
            )
        signal_batch = batches[signal_timeframe]
        signal = validate_decision(
            decision,
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            instrument=instrument,
            evaluated_at=now,
            last_closed_at=signal_batch.candles[-1].close_time,
        )
        signal = replace(signal, chart_enabled=chart_enabled)
        if not self.state.register(signal):
            self._advance_cursors(instrument.symbol, batches)
            return None
        self.state.transition(signal.signal_id, SignalStatus.VALIDATED)
        self._advance_cursors(instrument.symbol, batches)
        return self._deliver(signal, instrument, signal_batch, now)

    def retry_pending(self, instrument: InstrumentRules) -> tuple[TradeSignal, ...]:
        now = self.clock.now().astimezone(timezone.utc)
        delivered: list[TradeSignal] = []
        for signal in self.state.pending(now=now, symbol=instrument.symbol):
            if signal.expires_at is not None and signal.expires_at <= now:
                self.state.transition(signal.signal_id, SignalStatus.EXPIRED)
                continue
            batch = self.market_data.get_closed_candles(
                instrument, signal.signal_timeframe, 120, as_of=now
            )
            try:
                validate_signal(
                    signal,
                    instrument=instrument,
                    evaluated_at=now,
                    last_closed_at=batch.candles[-1].close_time,
                )
            except SignalValidationError as exc:
                self.state.transition(
                    signal.signal_id,
                    SignalStatus.CANCELLED,
                    error=f"retry_validation:{type(exc).__name__}",
                )
                LOGGER.warning(
                    "pending_signal_validation_failed",
                    extra={"signal_id": signal.signal_id, "symbol": signal.symbol},
                )
                continue
            status = self.state.status(signal.signal_id)
            if status is SignalStatus.DETECTED:
                self.state.transition(signal.signal_id, SignalStatus.VALIDATED)
            result = self._deliver(signal, instrument, batch, now)
            if result is not None:
                delivered.append(result)
        return tuple(delivered)

    def ingest(
        self,
        signal: TradeSignal,
        instrument: InstrumentRules,
        batch: CandleBatch,
    ) -> TradeSignal | None:
        """Consume an already-final Core event without evaluating a strategy."""
        now = self.clock.now().astimezone(timezone.utc)
        if signal.contract_version != "1.0" or signal.source != "core":
            raise ValueError("unsupported integrated signal contract")
        if batch.symbol != signal.symbol or batch.timeframe != signal.signal_timeframe:
            raise ValueError("integrated chart batch mismatch")
        validate_signal(
            signal,
            instrument=instrument,
            evaluated_at=now,
            last_closed_at=batch.candles[-1].close_time,
        )
        if not self.state.register(signal):
            return None
        self.state.transition(signal.signal_id, SignalStatus.VALIDATED)
        return self._deliver(signal, instrument, batch, now)

    def cancel(self, signal_id: str) -> None:
        current = self.state.status(signal_id)
        if current is None:
            raise ValueError(f"unknown signal: {signal_id}")
        if current in {SignalStatus.SENT, SignalStatus.CANCELLED, SignalStatus.EXPIRED}:
            raise ValueError(f"signal cannot be cancelled from {current.value}")
        self.state.transition(signal_id, SignalStatus.CANCELLED)
        signal = self.state.get_signal(signal_id)
        if signal is not None and self.lifecycle_notifier is not None:
            self.lifecycle_notifier.send_lifecycle(
                signal,
                SignalStatus.CANCELLED,
                format_lifecycle(signal, SignalStatus.CANCELLED),
            )

    def expire_pending(self) -> tuple[str, ...]:
        now = self.clock.now().astimezone(timezone.utc)
        expired: list[str] = []
        for signal in self.state.pending(now=now):
            if signal.expires_at is not None and signal.expires_at <= now:
                self.state.transition(signal.signal_id, SignalStatus.EXPIRED)
                if self.lifecycle_notifier is not None:
                    self.lifecycle_notifier.send_lifecycle(
                        signal,
                        SignalStatus.EXPIRED,
                        format_lifecycle(signal, SignalStatus.EXPIRED),
                    )
                expired.append(signal.signal_id)
        return tuple(expired)

    def _advance_cursors(self, symbol: str, batches: dict[str, CandleBatch]) -> None:
        for timeframe, batch in batches.items():
            self.state.set_last_processed(
                symbol,
                timeframe,
                batch.candles[-1].timestamp,
            )

    def _deliver(
        self,
        signal: TradeSignal,
        instrument: InstrumentRules,
        primary: CandleBatch,
        now: datetime,
    ) -> TradeSignal | None:
        chart: Path | None = None
        try:
            self.state.transition(signal.signal_id, SignalStatus.RENDERING)
            if signal.chart_enabled:
                try:
                    chart = self.renderer.render(signal, primary)
                except Exception as exc:
                    self.state.transition(
                        signal.signal_id,
                        SignalStatus.SEND_FAILED,
                        error=type(exc).__name__,
                        retry_at=now + timedelta(seconds=30),
                    )
                    raise
            self.state.transition(signal.signal_id, SignalStatus.READY)
            message = format_signal(signal, instrument)
            for attempt in range(1, self.max_send_attempts + 1):
                self.state.transition(signal.signal_id, SignalStatus.SENDING)
                try:
                    message_id = self.notifier.send(signal, message, chart)
                except Exception as exc:
                    retry_at = now + timedelta(seconds=min(60, 2**attempt))
                    self.state.transition(
                        signal.signal_id,
                        SignalStatus.SEND_FAILED,
                        error=type(exc).__name__,
                        retry_at=retry_at,
                    )
                    if attempt == self.max_send_attempts:
                        raise
                else:
                    self.state.transition(
                        signal.signal_id, SignalStatus.SENT, message_id=message_id
                    )
                    return signal
        finally:
            if chart is not None:
                try:
                    chart.unlink(missing_ok=True)
                except Exception:
                    LOGGER.warning(
                        "signal_chart_cleanup_failed",
                        extra={"signal_id": signal.signal_id, "path": str(chart)},
                        exc_info=True,
                    )
        return None
