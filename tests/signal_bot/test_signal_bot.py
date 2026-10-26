from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from modular_core import Candle, Side
from signal_bot.charts import MatplotlibChartRenderer
from signal_bot.domain import (
    CandleBatch,
    EntryType,
    InstrumentRules,
    SignalStatus,
    StrategyDecision,
)
from signal_bot.engine import SignalEngine
from signal_bot.formatter import format_lifecycle, format_signal
from signal_bot.orchestrator import InstrumentBinding, SignalOrchestrator
from signal_bot.state import SQLiteStateStore, StateTransitionError
from signal_bot.validation import SignalValidationError, validate_decision

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
RULES = InstrumentRules("BTCUSDT", "binance_usdt_m_perpetual", Decimal("0.1"))


def decision(**changes: object) -> StrategyDecision:
    values = dict(
        side=Side.BUY,
        signal_timestamp=NOW - timedelta(minutes=5),
        signal_timeframe="5m",
        entry_type=EntryType.LIMIT,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("99.0"),
        take_profit_levels=(Decimal("102.0"), Decimal("104.0")),
        reason_codes=("confirmed",),
        human_readable_reason="confirmed setup",
        expires_at=NOW + timedelta(hours=1),
        setup_id="setup-1",
    )
    values.update(changes)
    return StrategyDecision(**values)


def validated(**changes: object):
    return validate_decision(
        decision(**changes),
        strategy_id="test",
        strategy_version="1.0.0",
        instrument=RULES,
        evaluated_at=NOW,
        last_closed_at=NOW,
    )


def test_stable_id_and_risk_reward() -> None:
    first, second = validated(), validated()
    assert first.signal_id == second.signal_id
    assert first.risk_reward == (Decimal("2"), Decimal("4"))


@pytest.mark.parametrize(
    "changes",
    [
        {"stop_loss": Decimal("100.0")},
        {"stop_loss": Decimal("101.0")},
        {"entry_price": Decimal("100.05")},
        {"take_profit_levels": (Decimal("98.0"),)},
        {"expires_at": NOW},
        {"signal_timestamp": NOW + timedelta(minutes=5)},
        {"signal_timestamp": NOW - timedelta(minutes=11)},
        {"take_profit_levels": (Decimal("102.0"), Decimal("102.0"))},
        {"invalidation_price": Decimal("101.0")},
    ],
)
def test_invalid_levels_are_blocked(changes: dict[str, object]) -> None:
    with pytest.raises(SignalValidationError):
        validated(**changes)


def test_short_geometry() -> None:
    result = validated(
        side=Side.SELL,
        stop_loss=Decimal("101.0"),
        take_profit_levels=(Decimal("98.0"),),
    )
    assert result.risk_reward == (Decimal("2"),)


def test_formatter_contains_exact_levels_without_fake_confidence() -> None:
    message = format_signal(validated(), RULES)
    assert "ТВХ: 100.0" in message and "Стоп: 99.0" in message
    assert "TP2: 104.0 — 4.00R" in message
    assert "вероят" not in message.lower()
    assert "ОТМЕНЁН" in format_lifecycle(validated(), SignalStatus.CANCELLED)


def test_candle_batch_rejects_open_and_duplicate_bars() -> None:
    candle = Candle(NOW, 100, 101, 99, 100, symbol="BTCUSDT", timeframe="5m")
    with pytest.raises(ValueError, match="open candle"):
        CandleBatch("BTCUSDT", "5m", (candle,), NOW)
    closed = Candle(NOW - timedelta(minutes=5), 100, 101, 99, 100, symbol="BTCUSDT", timeframe="5m")
    with pytest.raises(ValueError, match="strictly ordered"):
        CandleBatch("BTCUSDT", "5m", (closed, closed), NOW)
    gap = Candle(NOW - timedelta(minutes=15), 100, 101, 99, 100, symbol="BTCUSDT", timeframe="5m")
    with pytest.raises(ValueError, match="gap"):
        CandleBatch("BTCUSDT", "5m", (gap, closed), NOW)


def test_sqlite_dedup_and_restart(tmp_path: Path) -> None:
    path = tmp_path / "state.sqlite3"
    store = SQLiteStateStore(path)
    signal = validated()
    assert store.register(signal) is True
    assert store.register(signal) is False
    store.transition(signal.signal_id, SignalStatus.VALIDATED)
    store.close()
    reopened = SQLiteStateStore(path)
    assert reopened.register(signal) is False
    assert reopened.status(signal.signal_id) is SignalStatus.VALIDATED
    with pytest.raises(StateTransitionError):
        reopened.transition(signal.signal_id, SignalStatus.SENT)
    reopened.close()


class FakeClock:
    def now(self) -> datetime:
        return NOW


class FakeStrategy:
    strategy_id = "test"
    version = "1.0.0"
    required_timeframes = ("5m",)

    def warmup_bars(self, timeframe: str) -> int:
        return 1

    def evaluate(self, context: object) -> StrategyDecision:
        return decision()


class FakeMarket:
    def get_closed_candles(
        self,
        instrument: InstrumentRules,
        timeframe: str,
        limit: int,
        *,
        as_of: datetime,
    ) -> CandleBatch:
        bars = tuple(
            Candle(
                NOW - timedelta(minutes=5 * i),
                100,
                101,
                99,
                100,
                symbol=instrument.symbol,
                timeframe=timeframe,
            )
            for i in range(limit, 0, -1)
        )
        return CandleBatch(instrument.symbol, timeframe, bars, as_of)


class FakeRenderer:
    def __init__(self, path: Path):
        self.path = path

    def render(self, signal: object, batch: object) -> Path:
        self.path.write_bytes(b"png")
        return self.path


class NeverRenderer:
    def render(self, signal: object, batch: object) -> Path:
        raise AssertionError("renderer must not be called")


class FlakyNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, signal: object, message: str, chart: Path) -> str:
        self.calls += 1
        if self.calls == 1:
            raise TimeoutError
        assert chart.exists() and "Signal ID" in message
        return "42"


class AlwaysFailNotifier:
    def send(self, signal: object, message: str, chart: Path) -> str:
        raise TimeoutError


class SuccessNotifier:
    def __init__(self) -> None:
        self.charts: list[Path | None] = []

    def send(self, signal: object, message: str, chart: Path | None) -> str:
        self.charts.append(chart)
        return "recovered-42"


class LifecycleRecorder:
    def __init__(self) -> None:
        self.statuses: list[SignalStatus] = []

    def send_lifecycle(self, signal: object, status: SignalStatus, message: str) -> str:
        assert "Signal ID" in message
        self.statuses.append(status)
        return "lifecycle-1"


def test_pipeline_retry_cleanup_and_no_duplicate(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    notifier = FlakyNotifier()
    chart = tmp_path / "chart.png"
    engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=FakeRenderer(chart),
        notifier=notifier,
        clock=FakeClock(),
    )
    signal = engine.process(RULES, FakeStrategy())
    assert signal and state.status(signal.signal_id) is SignalStatus.SENT
    assert notifier.calls == 2 and not chart.exists()
    assert engine.process(RULES, FakeStrategy()) is None
    state.close()


def test_text_only_mode_never_renders(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "text.db")
    notifier = SuccessNotifier()
    engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=NeverRenderer(),
        notifier=notifier,
        clock=FakeClock(),
    )
    signal = engine.process(RULES, FakeStrategy(), chart_enabled=False)
    assert signal is not None and signal.chart_enabled is False
    assert notifier.charts == [None]
    state.close()


def test_integrated_core_event_preserves_standalone_identity(tmp_path: Path) -> None:
    standalone = validated()
    integrated = replace(standalone, source="core", contract_version="1.0")
    batch = FakeMarket().get_closed_candles(RULES, "5m", 3, as_of=NOW)
    state = SQLiteStateStore(tmp_path / "integrated.db")
    engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=FakeRenderer(tmp_path / "integrated.png"),
        notifier=SuccessNotifier(),
        clock=FakeClock(),
    )
    delivered = engine.ingest(integrated, RULES, batch)
    assert delivered is not None and delivered.signal_id == standalone.signal_id
    assert delivered.source == "core"
    assert engine.ingest(integrated, RULES, batch) is None
    with pytest.raises(ValueError, match="contract"):
        engine.ingest(standalone, RULES, batch)
    state.close()


def test_persistent_outbox_recovers_after_restart(tmp_path: Path) -> None:
    database = tmp_path / "state.db"
    state = SQLiteStateStore(database)
    engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=FakeRenderer(tmp_path / "failed.png"),
        notifier=AlwaysFailNotifier(),
        clock=FakeClock(),
        max_send_attempts=1,
    )
    with pytest.raises(TimeoutError):
        engine.process(RULES, FakeStrategy())
    signal = validated()
    assert state.status(signal.signal_id) is SignalStatus.SEND_FAILED
    state.close()

    state = SQLiteStateStore(database)
    recovered_engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=FakeRenderer(tmp_path / "recovered.png"),
        notifier=SuccessNotifier(),
        clock=FakeClock(),
    )
    later = FakeClock()
    later.now = lambda: NOW + timedelta(seconds=3)  # type: ignore[method-assign]
    recovered_engine.clock = later
    recovered = recovered_engine.retry_pending(RULES)
    assert len(recovered) == 1
    assert state.status(signal.signal_id) is SignalStatus.SENT
    state.close()


def test_cancel_and_expire_lifecycle(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    signal = validated()
    assert state.register(signal)
    state.transition(signal.signal_id, SignalStatus.VALIDATED)
    recorder = LifecycleRecorder()
    engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=FakeRenderer(tmp_path / "chart.png"),
        notifier=SuccessNotifier(),
        clock=FakeClock(),
        lifecycle_notifier=recorder,
    )
    engine.cancel(signal.signal_id)
    assert state.status(signal.signal_id) is SignalStatus.CANCELLED
    with pytest.raises(ValueError, match="cannot be cancelled"):
        engine.cancel(signal.signal_id)

    expiring = validated(expires_at=NOW + timedelta(seconds=1), setup_id="expiring")
    assert state.register(expiring)
    state.transition(expiring.signal_id, SignalStatus.VALIDATED)
    later = FakeClock()
    later.now = lambda: NOW + timedelta(seconds=2)  # type: ignore[method-assign]
    engine.clock = later
    assert engine.expire_pending() == (expiring.signal_id,)
    assert state.status(expiring.signal_id) is SignalStatus.EXPIRED
    assert recorder.statuses == [SignalStatus.CANCELLED, SignalStatus.EXPIRED]
    state.close()


def test_pair_failure_isolation_is_possible(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    engine = SignalEngine(
        market_data=FakeMarket(),
        state=state,
        renderer=FakeRenderer(tmp_path / "chart.png"),
        notifier=FlakyNotifier(),
        clock=FakeClock(),
    )
    bad = InstrumentRules("BADUSDT", "binance_usdt_m_perpetual", Decimal("0.3"))
    with pytest.raises(SignalValidationError):
        engine.process(bad, FakeStrategy())
    assert state.last_processed("BADUSDT", "5m") is None
    assert engine.process(RULES, FakeStrategy()) is not None
    state.close()


@pytest.mark.parametrize(
    ("side", "entry", "stop", "targets", "tick"),
    [
        (Side.BUY, "118420.5", "117380.0", ("120501.5",), "0.1"),
        (Side.SELL, "0.00001230", "0.00001260", ("0.00001170",), "0.00000001"),
    ],
)
def test_chart_reference_scenarios(
    side: Side,
    entry: str,
    stop: str,
    targets: tuple[str, ...],
    tick: str,
) -> None:
    rules = InstrumentRules("TESTUSDT", "binance_usdt_m_perpetual", Decimal(tick))
    base = float(Decimal(entry))
    bars = tuple(
        Candle(
            NOW - timedelta(minutes=5 * i),
            base * 0.999,
            base * 1.002,
            base * 0.998,
            base,
            symbol="TESTUSDT",
            timeframe="5m",
        )
        for i in range(60, 0, -1)
    )
    batch = CandleBatch("TESTUSDT", "5m", bars, NOW)
    signal = validate_decision(
        decision(
            side=side,
            entry_price=Decimal(entry),
            stop_loss=Decimal(stop),
            take_profit_levels=tuple(Decimal(value) for value in targets),
        ),
        strategy_id="chart-test",
        strategy_version="1.0.0",
        instrument=rules,
        evaluated_at=NOW,
        last_closed_at=NOW,
    )
    output = MatplotlibChartRenderer(width=1200, height=675).render(signal, batch)
    try:
        assert output.read_bytes().startswith(b"\x89PNG")
        assert 20_000 < output.stat().st_size < 5_000_000
    finally:
        output.unlink(missing_ok=True)


class SelectiveEngine:
    def retry_pending(self, instrument: InstrumentRules):
        return ()

    def process(
        self,
        instrument: InstrumentRules,
        strategy: FakeStrategy,
        *,
        chart_enabled: bool = True,
    ):
        if instrument.symbol == "BADUSDT":
            raise RuntimeError("isolated")
        return validated()


def test_orchestrator_isolates_pairs_and_reports_health() -> None:
    good = RULES
    bad = InstrumentRules("BADUSDT", "binance_usdt_m_perpetual", Decimal("0.1"))
    seen: list[str] = []
    orchestrator = SignalOrchestrator(
        SelectiveEngine(),  # type: ignore[arg-type]
        (
            InstrumentBinding(bad, FakeStrategy()),
            InstrumentBinding(good, FakeStrategy()),
        ),
        on_error=lambda symbol, error: seen.append(symbol),
    )
    result = orchestrator.run_cycle()
    assert result.failed_symbols == ("BADUSDT",)
    assert len(result.signals) == 1 and seen == ["BADUSDT"]
    assert orchestrator.health()["status"] == "degraded"
    orchestrator.stop()
