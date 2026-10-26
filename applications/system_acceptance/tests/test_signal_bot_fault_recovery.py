from __future__ import annotations

import socket
import sqlite3
import urllib.error
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

try:
    from modular_core import Candle, Side
    from modular_observability import (
        Component,
        ComponentHealth,
        ObservabilityPolicy,
        UnavailableSource,
        project_component,
    )
    from signal_bot.adapters import TelegramNotifier
    from signal_bot.domain import (
        CandleBatch,
        EntryType,
        InstrumentRules,
        SignalStatus,
        StrategyDecision,
    )
    from signal_bot.engine import SignalEngine
    from signal_bot.state import SQLiteStateStore
except ModuleNotFoundError as exc:
    pytest.skip(f"Signal Bot recovery packages unavailable: {exc.name}", allow_module_level=True)

from modular_system_acceptance.fault_recovery import (
    assert_recovered_state_matches,
    load_fault_recovery_matrix,
)

NOW = datetime(2026, 8, 28, 7, 0, tzinfo=UTC)
RULES = InstrumentRules("BTCUSDT", "binance_usdt_m_perpetual", Decimal("0.1"))
MATRIX_PATH = Path(__file__).resolve().parents[1] / "fault_recovery_matrix.v1.json"


class FixedClock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def now(self) -> datetime:
        return self.moment


class StaticMarket:
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
                NOW - timedelta(minutes=5 * index),
                100,
                101,
                99,
                100,
                symbol=instrument.symbol,
                timeframe=timeframe,
            )
            for index in range(limit, 0, -1)
        )
        return CandleBatch(instrument.symbol, timeframe, bars, as_of)


class DeterministicStrategy:
    strategy_id = "fault-recovery-signal-bot"
    version = "1.0.0"
    required_timeframes = ("5m",)

    def warmup_bars(self, timeframe: str) -> int:
        assert timeframe == "5m"
        return 1

    def evaluate(self, context: object) -> StrategyDecision:
        del context
        return StrategyDecision(
            side=Side.BUY,
            signal_timestamp=NOW - timedelta(minutes=5),
            signal_timeframe="5m",
            entry_type=EntryType.LIMIT,
            entry_price=Decimal("100"),
            stop_loss=Decimal("99"),
            take_profit_levels=(Decimal("102"),),
            reason_codes=("fault_recovery",),
            human_readable_reason="Signal Bot transport recovery acceptance",
            expires_at=NOW + timedelta(hours=1),
            setup_id="fault-recovery-signal-bot-1",
        )


class NeverRenderer:
    def render(self, signal: object, batch: object) -> Path:
        del signal, batch
        raise AssertionError("text-only Signal Bot recovery must not render a chart")


class CountingSuccessNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, signal: object, message: str, chart: Path | None) -> str:
        del signal
        assert message
        assert chart is None
        self.calls += 1
        return f"accepted-message-{self.calls}"


def _factual_state(path: Path, state: SQLiteStateStore) -> dict[str, object]:
    cursor = state.last_processed(RULES.symbol, "5m")
    assert cursor is not None
    with sqlite3.connect(path) as connection:
        outbox_row = connection.execute(
            "SELECT value FROM state_counters WHERE name='total_signals'"
        ).fetchone()
        sent_row = connection.execute(
            "SELECT COUNT(*) FROM signals WHERE status=?",
            (SignalStatus.SENT.value,),
        ).fetchone()
    assert outbox_row is not None
    assert sent_row is not None
    return {
        "signal_outbox_count": int(outbox_row[0]),
        "signal_cursor": cursor.isoformat(),
        "notification_count": int(sent_row[0]),
    }


def _continuous_state(path: Path) -> dict[str, object]:
    state = SQLiteStateStore(path)
    notifier = CountingSuccessNotifier()
    engine = SignalEngine(
        market_data=StaticMarket(),
        state=state,
        renderer=NeverRenderer(),
        notifier=notifier,
        clock=FixedClock(NOW),
        max_send_attempts=1,
    )
    signal = engine.process(RULES, DeterministicStrategy(), chart_enabled=False)
    assert signal is not None
    assert state.status(signal.signal_id) is SignalStatus.SENT
    assert notifier.calls == 1
    factual = _factual_state(path, state)
    state.close()
    return factual


def _recovered_state(path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    refused_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    refused_socket.bind(("127.0.0.1", 0))
    port = int(refused_socket.getsockname()[1])

    state = SQLiteStateStore(path)
    notifier = TelegramNotifier(
        "fault-recovery-test-token", "fault-recovery-test-chat", timeout=0.5
    )
    notifier.url = f"https://127.0.0.1:{port}/bot-fault-recovery/sendPhoto"
    engine = SignalEngine(
        market_data=StaticMarket(),
        state=state,
        renderer=NeverRenderer(),
        notifier=notifier,
        clock=FixedClock(NOW),
        max_send_attempts=1,
    )
    try:
        with pytest.raises(urllib.error.URLError) as transport_error:
            engine.process(RULES, DeterministicStrategy(), chart_enabled=False)
    finally:
        refused_socket.close()
    assert isinstance(transport_error.value.reason, OSError)

    signal = state.latest_signal(RULES.symbol)
    assert signal is not None
    assert state.status(signal.signal_id) is SignalStatus.SEND_FAILED
    failed_state = _factual_state(path, state)
    assert failed_state["signal_outbox_count"] == 1
    assert failed_state["notification_count"] == 0

    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("signal_bot_unavailable")
    assert scenario.risk_supervisor.state == "healthy"
    assert scenario.risk_supervisor.guard_code is None
    projection = project_component(
        UnavailableSource(component=Component.SIGNAL_BOT).read(now=NOW),
        now=NOW,
        policy=ObservabilityPolicy.paper_default(freshness_seconds=30.0),
    )
    assert projection.health is ComponentHealth.UNAVAILABLE
    assert "owner_unavailable" in projection.reason_codes
    state.close()

    recovered_state = SQLiteStateStore(path)
    success = CountingSuccessNotifier()
    recovered_engine = SignalEngine(
        market_data=StaticMarket(),
        state=recovered_state,
        renderer=NeverRenderer(),
        notifier=success,
        clock=FixedClock(NOW + timedelta(seconds=3)),
        max_send_attempts=1,
    )
    recovered = recovered_engine.retry_pending(RULES)
    assert recovered == (signal,)
    assert success.calls == 1
    assert recovered_state.status(signal.signal_id) is SignalStatus.SENT
    assert recovered_engine.retry_pending(RULES) == ()
    assert success.calls == 1
    factual = _factual_state(path, recovered_state)
    recovered_state.close()
    return factual


def test_real_signal_bot_transport_failure_recovers_from_owner_outbox_without_duplicates(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = load_fault_recovery_matrix(MATRIX_PATH).scenario("signal_bot_unavailable")
    continuous = _continuous_state(tmp_path / "continuous.sqlite3")
    recovered = _recovered_state(tmp_path / "recovered.sqlite3", monkeypatch)

    assert continuous["signal_outbox_count"] == 1
    assert continuous["notification_count"] == 1
    assert recovered["signal_outbox_count"] == 1
    assert recovered["notification_count"] == 1
    left, right = assert_recovered_state_matches(
        continuous,
        recovered,
        fields=scenario.factual_state_fields,
    )
    assert left == right
