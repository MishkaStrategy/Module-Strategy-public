from __future__ import annotations

from decimal import Decimal
from pathlib import Path
import urllib.error
from unittest.mock import patch

from signal_bot.control import (
    PreferenceAwareNotifier,
    TelegramCommandService,
    TelegramControlLoop,
)
from signal_bot.adapters import TelegramBotClient
from signal_bot.domain import InstrumentRules
from signal_bot.orchestrator import InstrumentBinding
from signal_bot.state import SQLiteStateStore


class Strategy:
    strategy_id = "btc-strategy"
    version = "1.2.0"


class Orchestrator:
    bindings = (
        InstrumentBinding(
            InstrumentRules("BTCUSDT", "binance_usdt_m_perpetual", Decimal("0.1")),
            Strategy(),  # type: ignore[arg-type]
        ),
    )

    def health(self) -> dict[str, object]:
        return {"status": "ok", "cycles": 3, "signals_sent": 1, "pair_errors": 0}


class Client:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def get_updates(self, *, offset: int | None = None, poll_timeout: int = 20):
        if offset is not None:
            return []
        return [{"update_id": 7, "message": {"chat": {"id": 42}, "text": "/pairs"}}]

    def send_text(self, chat_id: str, text: str) -> str:
        self.sent.append((chat_id, text))
        return "1"


def test_commands_preferences_admin_and_polling(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    service = TelegramCommandService(
        Orchestrator(),
        state,
        frozenset({"42"}),  # type: ignore[arg-type]
        frozenset({"42", "13"}),
        "42",
    )
    assert "BTCUSDT" in service.handle("42", "/pairs")
    assert "Статус: ok" in service.handle("42", "/status")
    assert service.handle("13", "/health") == "Команда доступна только администратору"
    assert service.handle("99", "/status") == ""
    assert service.handle("42", "hello") == ""
    assert "status" in service.handle("42", "/health")
    assert service.handle("42", "/notifications_off") == "Уведомления отключены"
    assert state.notifications_enabled("42") is False
    assert service.handle("42", "/notifications_on") == "Уведомления включены"
    assert state.notifications_enabled("42") is True
    assert "основному получателю" in service.handle("13", "/notifications_off")
    assert state.notifications_enabled("42") is True
    assert service.handle("42", "/last") == "Сигналов пока нет"
    client = Client()
    loop = TelegramControlLoop(client, service)
    assert loop.run_once() == 1
    assert client.sent and "BTCUSDT" in client.sent[0][1]
    assert loop.run_once() == 0
    loop.stop()
    state.close()

    reopened = SQLiteStateStore(tmp_path / "state.db")
    restarted_service = TelegramCommandService(
        Orchestrator(),
        reopened,
        frozenset({"42"}),
        frozenset({"42"}),
        "42",  # type: ignore[arg-type]
    )
    restarted_client = Client()
    restarted = TelegramControlLoop(restarted_client, restarted_service)
    assert restarted.run_once() == 0
    assert reopened.telegram_update_offset() == 8
    reopened.close()


def test_preference_notifier_suppresses_delivery(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    state.set_notifications("42", False)
    delegate = SimpleDelegate()
    notifier = PreferenceAwareNotifier(delegate, state, "42")  # type: ignore[arg-type]
    result = notifier.send(None, "message", tmp_path / "chart.png")  # type: ignore[arg-type]
    assert result == "notifications-disabled" and delegate.calls == 0
    state.close()


def test_control_poll_log_does_not_expose_telegram_token(tmp_path: Path, caplog) -> None:
    state = SQLiteStateStore(tmp_path / "state.db")
    service = TelegramCommandService(
        Orchestrator(),
        state,
        frozenset(),
        frozenset({"42"}),
        "42",  # type: ignore[arg-type]
    )
    client = TelegramBotClient("TOPSECRET")
    loop = TelegramControlLoop(client, service)
    loop._stop.wait = lambda timeout=None: loop.stop()  # type: ignore[method-assign]

    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.HTTPError(
            "https://api.telegram.org/botTOPSECRET/getUpdates", 503, "unavailable", None, None
        ),
    ):
        loop.run_forever()

    assert "TOPSECRET" not in caplog.text
    assert "api.telegram.org" not in caplog.text
    state.close()


class SimpleDelegate:
    def __init__(self) -> None:
        self.calls = 0

    def send(self, signal: object, message: str, chart: Path) -> str:
        self.calls += 1
        return "sent"
