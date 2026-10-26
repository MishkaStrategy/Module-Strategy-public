from __future__ import annotations

import logging
import signal
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import monotonic
from typing import Callable, Iterable

from .contracts import SignalStrategy
from .domain import InstrumentRules, TradeSignal
from .engine import SignalEngine

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class InstrumentBinding:
    instrument: InstrumentRules
    strategy: SignalStrategy
    chart_enabled: bool = True


@dataclass(slots=True)
class RuntimeMetrics:
    cycles: int = 0
    pairs_processed: int = 0
    signals_sent: int = 0
    pair_errors: int = 0
    processing_seconds: float = 0.0
    last_cycle_at: datetime | None = None
    last_success_at: datetime | None = None
    errors_by_symbol: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class CycleResult:
    signals: tuple[TradeSignal, ...]
    failed_symbols: tuple[str, ...]


class SignalOrchestrator:
    """Runs independent pair boundaries and supports graceful periodic polling."""

    def __init__(
        self,
        engine: SignalEngine,
        bindings: Iterable[InstrumentBinding],
        *,
        poll_seconds: float = 15.0,
        on_error: Callable[[str, Exception], None] | None = None,
    ) -> None:
        self.engine = engine
        self.bindings = tuple(bindings)
        if not self.bindings:
            raise ValueError("at least one enabled instrument is required")
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        symbols = [item.instrument.symbol for item in self.bindings]
        if len(symbols) != len(set(symbols)):
            raise ValueError("instrument symbols must be unique")
        self.poll_seconds = poll_seconds
        self.on_error = on_error
        self.metrics = RuntimeMetrics()
        self._stop = threading.Event()

    def run_cycle(self) -> CycleResult:
        started = monotonic()
        signals: list[TradeSignal] = []
        failed: list[str] = []
        for binding in self.bindings:
            symbol = binding.instrument.symbol
            try:
                recovered = self.engine.retry_pending(binding.instrument)
                signals.extend(recovered)
                self.metrics.signals_sent += len(recovered)
                result = self.engine.process(
                    binding.instrument,
                    binding.strategy,
                    chart_enabled=binding.chart_enabled,
                )
            except Exception as exc:
                failed.append(symbol)
                self.metrics.pair_errors += 1
                self.metrics.errors_by_symbol[symbol] = type(exc).__name__
                LOGGER.exception("pair_cycle_failed", extra={"symbol": symbol})
                if self.on_error is not None:
                    self.on_error(symbol, exc)
            else:
                self.metrics.pairs_processed += 1
                self.metrics.errors_by_symbol.pop(symbol, None)
                self.metrics.last_success_at = datetime.now(timezone.utc)
                if result is not None:
                    signals.append(result)
                    self.metrics.signals_sent += 1
        self.metrics.cycles += 1
        self.metrics.last_cycle_at = datetime.now(timezone.utc)
        self.metrics.processing_seconds += monotonic() - started
        return CycleResult(tuple(signals), tuple(failed))

    def run_forever(self) -> None:
        self._install_signal_handlers()
        while not self._stop.is_set():
            self.run_cycle()
            self._stop.wait(self.poll_seconds)

    def stop(self) -> None:
        self._stop.set()

    def health(self) -> dict[str, object]:
        return {
            "status": "degraded" if self.metrics.errors_by_symbol else "ok",
            "cycles": self.metrics.cycles,
            "pairs_processed": self.metrics.pairs_processed,
            "signals_sent": self.metrics.signals_sent,
            "pair_errors": self.metrics.pair_errors,
            "last_cycle_at": self.metrics.last_cycle_at,
            "last_success_at": self.metrics.last_success_at,
            "errors_by_symbol": dict(self.metrics.errors_by_symbol),
        }

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGINT, lambda *_: self.stop())
        signal.signal(signal.SIGTERM, lambda *_: self.stop())
