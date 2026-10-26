from __future__ import annotations

import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from modular_core import Candle, Side
from signal_bot.config import ConfigurationError, load_config
from signal_bot.control import TelegramControlLoop
from signal_bot.domain import (
    CandleBatch,
    EntryType,
    InstrumentRules,
    SignalStatus,
    StrategyContext,
    StrategyDecision,
    TradeSignal,
    signal_to_dict,
)
from signal_bot.engine import SignalEngine
from signal_bot.state import SQLiteStateStore
from signal_bot.validation import validate_decision

UTC = timezone.utc
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)
RULES = InstrumentRules(
    "BTCUSDT",
    "binance_usdt_m_perpetual",
    Decimal("0.1"),
)


def _config_text(*, enabled: str = "true", chart_enabled: str = "true") -> str:
    return f"""
state:
  database: data/state.sqlite3
instruments:
  BTCUSDT:
    enabled: {enabled}
    market: binance_usdt_m_perpetual
    tick_size: "0.1"
    strategy:
      id: test
      version: "1.0.0"
      parameters: {{}}
    notifications:
      chart_enabled: {chart_enabled}
"""


@pytest.mark.parametrize(
    ("enabled", "chart_enabled"),
    [
        ('"false"', "true"),
        ('"0"', "true"),
        ('"no"', "true"),
        ("true", '"false"'),
        ("true", '"0"'),
    ],
)
def test_yaml_boolean_strings_fail_closed(
    tmp_path: Path,
    enabled: str,
    chart_enabled: str,
) -> None:
    path = tmp_path / "bot.yaml"
    path.write_text(
        _config_text(enabled=enabled, chart_enabled=chart_enabled),
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="must be a boolean"):
        load_config(path)


def test_relative_database_identity_is_config_relative(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    path = config_dir / "bot.yaml"
    path.write_text(_config_text(), encoding="utf-8")
    other_cwd = tmp_path / "other"
    other_cwd.mkdir()
    monkeypatch.chdir(other_cwd)

    config = load_config(path)

    assert config.database == (config_dir / "data/state.sqlite3").resolve()
    assert config.database != (other_cwd / "data/state.sqlite3").resolve()


def test_late_older_cursor_cannot_overwrite_newer_value(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    newer = NOW
    older = NOW - timedelta(minutes=5)
    start = threading.Event()
    newer_committed = threading.Event()

    def write_newer() -> None:
        start.wait(timeout=2)
        state.set_last_processed("BTCUSDT", "5m", newer)
        newer_committed.set()

    def write_older_late() -> None:
        start.wait(timeout=2)
        newer_committed.wait(timeout=2)
        state.set_last_processed("BTCUSDT", "5m", older)

    threads = [threading.Thread(target=write_newer), threading.Thread(target=write_older_late)]
    for thread in threads:
        thread.start()
    start.set()
    for thread in threads:
        thread.join(timeout=5)
        assert not thread.is_alive()

    assert state.last_processed("BTCUSDT", "5m") == newer
    state.close()


def _decision(**changes: object) -> StrategyDecision:
    values: dict[str, object] = {
        "side": Side.BUY,
        "signal_timestamp": NOW - timedelta(minutes=5),
        "signal_timeframe": "5m",
        "entry_type": EntryType.LIMIT,
        "entry_price": Decimal("100.0"),
        "stop_loss": Decimal("99.0"),
        "take_profit_levels": (Decimal("102.0"),),
        "reason_codes": ("confirmed",),
        "human_readable_reason": "confirmed setup",
        "expires_at": NOW + timedelta(hours=1),
        "setup_id": "setup-1",
    }
    values.update(changes)
    return StrategyDecision(**values)


def _valid_signal(**changes: object) -> TradeSignal:
    return validate_decision(
        _decision(**changes),
        strategy_id="test",
        strategy_version="1.0.0",
        instrument=RULES,
        evaluated_at=NOW,
        last_closed_at=NOW,
    )


def _batch(
    timeframe: str,
    *,
    latest: datetime,
    count: int = 3,
    symbol: str = "BTCUSDT",
    observed_at: datetime = NOW,
) -> CandleBatch:
    interval = timedelta(minutes=5) if timeframe == "5m" else timedelta(hours=1)
    candles = tuple(
        Candle(
            latest - interval * index,
            100,
            101,
            99,
            100,
            symbol=symbol,
            timeframe=timeframe,
        )
        for index in range(count - 1, -1, -1)
    )
    return CandleBatch(symbol, timeframe, candles, observed_at)


class FixedClock:
    def __init__(self, now: datetime = NOW) -> None:
        self.value = now

    def now(self) -> datetime:
        return self.value


class FixedMarket:
    def get_closed_candles(
        self,
        instrument: InstrumentRules,
        timeframe: str,
        limit: int,
        *,
        as_of: datetime,
    ) -> CandleBatch:
        latest = NOW - timedelta(minutes=5) if timeframe == "5m" else NOW - timedelta(hours=1)
        return _batch(
            timeframe,
            latest=latest,
            count=limit,
            symbol=instrument.symbol,
            observed_at=as_of,
        )


class NeverRenderer:
    def render(self, signal: TradeSignal, batch: CandleBatch) -> Path:
        raise AssertionError("renderer must not be called")


class FileRenderer:
    def __init__(self, path: Path) -> None:
        self.path = path

    def render(self, signal: TradeSignal, batch: CandleBatch) -> Path:
        self.path.write_bytes(b"chart")
        return self.path


class RecordingNotifier:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, signal: TradeSignal, message: str, chart: Path | None) -> str:
        self.calls += 1
        return "message-1"


def test_corrupt_persisted_payload_is_cancelled_without_delivery(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    signal = _valid_signal()
    assert state.register(signal)
    payload = signal_to_dict(signal)
    payload["chart_enabled"] = "false"
    with state._lock, state._db:
        state._db.execute(
            "UPDATE signals SET payload=? WHERE signal_id=?",
            (json.dumps(payload), signal.signal_id),
        )
    notifier = RecordingNotifier()
    engine = SignalEngine(
        market_data=FixedMarket(),
        state=state,
        renderer=NeverRenderer(),
        notifier=notifier,
        clock=FixedClock(),
    )

    assert engine.retry_pending(RULES) == ()
    assert state.status(signal.signal_id) is SignalStatus.CANCELLED
    assert notifier.calls == 0
    state.close()


def test_retry_revalidates_current_instrument_geometry(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    signal = _valid_signal()
    assert state.register(signal)
    state.transition(signal.signal_id, SignalStatus.VALIDATED)
    notifier = RecordingNotifier()
    engine = SignalEngine(
        market_data=FixedMarket(),
        state=state,
        renderer=NeverRenderer(),
        notifier=notifier,
        clock=FixedClock(),
    )
    changed_rules = InstrumentRules(
        "BTCUSDT",
        "binance_usdt_m_perpetual",
        Decimal("0.3"),
    )

    assert engine.retry_pending(changed_rules) == ()
    assert state.status(signal.signal_id) is SignalStatus.CANCELLED
    assert notifier.calls == 0
    state.close()


@pytest.mark.parametrize("attempts", [0, -1, True])
def test_non_positive_send_attempts_are_rejected(
    tmp_path: Path,
    attempts: int,
) -> None:
    state = SQLiteStateStore(tmp_path / f"state-{attempts}.sqlite3")
    with pytest.raises(ValueError, match="positive integer"):
        SignalEngine(
            market_data=FixedMarket(),
            state=state,
            renderer=NeverRenderer(),
            notifier=RecordingNotifier(),
            max_send_attempts=attempts,
        )
    state.close()


def test_chart_cleanup_failure_does_not_mask_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    chart = tmp_path / "chart.png"
    notifier = RecordingNotifier()
    engine = SignalEngine(
        market_data=FixedMarket(),
        state=state,
        renderer=FileRenderer(chart),
        notifier=notifier,
        clock=FixedClock(),
    )
    original_unlink = Path.unlink

    def failing_unlink(path: Path, *args: object, **kwargs: object) -> None:
        if path == chart:
            raise OSError("cleanup failure")
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", failing_unlink)

    delivered = engine.ingest(
        replace(_valid_signal(), source="core"),
        RULES,
        _batch("5m", latest=NOW - timedelta(minutes=5)),
    )

    assert delivered is not None
    assert state.status(delivered.signal_id) is SignalStatus.SENT
    assert notifier.calls == 1
    state.close()


class OffsetService:
    def __init__(self, state: SQLiteStateStore) -> None:
        self.state = state
        self.fail = True

    def handle(self, chat_id: str, text: str) -> str:
        if self.fail:
            raise RuntimeError("handler failed")
        return "ok"


class OffsetClient:
    def __init__(self) -> None:
        self.fail_send = False

    def get_updates(
        self, *, offset: int | None = None, poll_timeout: int = 20
    ) -> list[dict[str, object]]:
        return [{"update_id": 7, "message": {"chat": {"id": 42}, "text": "/status"}}]

    def send_text(self, chat_id: str, text: str) -> str:
        if self.fail_send:
            raise RuntimeError("send failed")
        return "reply-1"


def test_telegram_offset_commits_only_after_handler_and_reply_success(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    service = OffsetService(state)
    client = OffsetClient()
    loop = TelegramControlLoop(client, service)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="handler failed"):
        loop.run_once()
    assert state.telegram_update_offset() is None

    service.fail = False
    client.fail_send = True
    with pytest.raises(RuntimeError, match="send failed"):
        loop.run_once()
    assert state.telegram_update_offset() is None

    client.fail_send = False
    assert loop.run_once() == 1
    assert state.telegram_update_offset() == 8
    state.close()


class MultiTimeframeMarket:
    def __init__(self) -> None:
        self.latest = {
            "5m": NOW - timedelta(minutes=5),
            "1h": NOW - timedelta(hours=2),
        }

    def get_closed_candles(
        self,
        instrument: InstrumentRules,
        timeframe: str,
        limit: int,
        *,
        as_of: datetime,
    ) -> CandleBatch:
        return _batch(
            timeframe,
            latest=self.latest[timeframe],
            count=limit,
            symbol=instrument.symbol,
            observed_at=as_of,
        )


class NoSignalMultiTimeframeStrategy:
    strategy_id = "multi"
    version = "1.0.0"
    required_timeframes = ("5m", "1h")

    def __init__(self) -> None:
        self.calls = 0

    def warmup_bars(self, timeframe: str) -> int:
        return 1

    def evaluate(self, context: StrategyContext) -> StrategyDecision:
        self.calls += 1
        return StrategyDecision()


def test_htf_only_advancement_triggers_new_evaluation(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.sqlite3")
    market = MultiTimeframeMarket()
    strategy = NoSignalMultiTimeframeStrategy()
    engine = SignalEngine(
        market_data=market,
        state=state,
        renderer=NeverRenderer(),
        notifier=RecordingNotifier(),
        clock=FixedClock(),
    )

    assert engine.process(RULES, strategy) is None
    assert strategy.calls == 1
    primary_cursor = state.last_processed("BTCUSDT", "5m")

    market.latest["1h"] = NOW - timedelta(hours=1)
    assert engine.process(RULES, strategy) is None
    assert strategy.calls == 2
    assert state.last_processed("BTCUSDT", "5m") == primary_cursor
    assert state.last_processed("BTCUSDT", "1h") == NOW - timedelta(hours=1)
    state.close()
