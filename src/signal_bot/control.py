from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .contracts import Notifier
from .domain import SignalStatus, TradeSignal
from .orchestrator import SignalOrchestrator
from .state import SQLiteStateStore

LOGGER = logging.getLogger(__name__)


class PreferenceAwareNotifier:
    def __init__(self, delegate: Notifier, state: SQLiteStateStore, chat_id: str) -> None:
        self.delegate, self.state, self.chat_id = delegate, state, chat_id

    def send(self, signal: TradeSignal, message: str, chart: Path | None) -> str:
        if not self.state.notifications_enabled(self.chat_id):
            return "notifications-disabled"
        return self.delegate.send(signal, message, chart)


class TelegramLifecycleNotifier:
    def __init__(
        self,
        client: BotClient,
        state: SQLiteStateStore,
        chat_id: str,
    ) -> None:
        self.client, self.state, self.chat_id = client, state, chat_id

    def send_lifecycle(self, signal: TradeSignal, status: SignalStatus, message: str) -> str:
        if not self.state.notifications_enabled(self.chat_id):
            return "notifications-disabled"
        return self.client.send_text(self.chat_id, message)


class BotClient(Protocol):
    def get_updates(
        self, *, offset: int | None = None, poll_timeout: int = 20
    ) -> list[dict[str, object]]: ...
    def send_text(self, chat_id: str, text: str) -> str: ...


@dataclass(slots=True)
class TelegramCommandService:
    orchestrator: SignalOrchestrator
    state: SQLiteStateStore
    admin_chat_ids: frozenset[str] = frozenset()
    allowed_chat_ids: frozenset[str] = frozenset()
    primary_recipient: str = ""

    def handle(self, chat_id: str, text: str) -> str:
        if chat_id not in self.allowed_chat_ids:
            return ""
        if not text.strip().startswith("/"):
            return ""
        command = text.strip().split(maxsplit=1)[0].split("@", 1)[0].lower()
        if command == "/start":
            return "Модульный сигнальный бот активен. Команды: /status /pairs /last /notifications_on /notifications_off"
        if command == "/status":
            health = self.orchestrator.health()
            return (
                f"Статус: {health['status']}\nЦиклов: {health['cycles']}\n"
                f"Сигналов: {health['signals_sent']}\nОшибок пар: {health['pair_errors']}"
            )
        if command == "/pairs":
            return "Активные пары:\n" + "\n".join(
                f"• {item.instrument.symbol} — {item.strategy.strategy_id} v{item.strategy.version}"
                for item in self.orchestrator.bindings
            )
        if command == "/last":
            signal = self.state.latest_signal()
            return (
                "Сигналов пока нет"
                if signal is None
                else (
                    f"Последний сигнал: {signal.side.value.upper()} {signal.symbol}\n"
                    f"Entry {signal.entry_price} • Stop {signal.stop_loss}\n{signal.signal_id}"
                )
            )
        if command == "/notifications_on":
            if chat_id != self.primary_recipient:
                return "Управление уведомлениями доступно только основному получателю"
            self.state.set_notifications(self.primary_recipient, True)
            return "Уведомления включены"
        if command == "/notifications_off":
            if chat_id != self.primary_recipient:
                return "Управление уведомлениями доступно только основному получателю"
            self.state.set_notifications(self.primary_recipient, False)
            return "Уведомления отключены"
        if command == "/health":
            if chat_id not in self.admin_chat_ids:
                return "Команда доступна только администратору"
            return repr(self.orchestrator.health())
        return "Неизвестная команда. Используйте /start"


class TelegramControlLoop:
    def __init__(self, client: BotClient, service: TelegramCommandService) -> None:
        self.client, self.service = client, service
        self._offset = service.state.telegram_update_offset()
        self._stop = threading.Event()

    @staticmethod
    def _update_id(update: dict[str, object]) -> int | None:
        value = update.get("update_id")
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            return None
        try:
            update_id = int(value)
        except ValueError:
            return None
        return update_id if update_id >= 0 else None

    def _commit_offset(self, update_id: int) -> None:
        next_offset = update_id + 1
        self.service.state.set_telegram_update_offset(next_offset)
        self._offset = next_offset

    def run_once(self) -> int:
        handled = 0
        updates = sorted(
            self.client.get_updates(offset=self._offset),
            key=lambda item: self._update_id(item) or -1,
        )
        for update in updates:
            update_id = self._update_id(update)
            if update_id is None:
                continue
            if self._offset is not None and update_id < self._offset:
                continue
            message = update.get("message")
            if not isinstance(message, dict):
                self._commit_offset(update_id)
                continue
            chat = message.get("chat")
            text = message.get("text")
            if not isinstance(chat, dict) or not isinstance(text, str) or "id" not in chat:
                self._commit_offset(update_id)
                continue
            chat_id = str(chat["id"])
            response = self.service.handle(chat_id, text)
            if response:
                self.client.send_text(chat_id, response)
                handled += 1
            self._commit_offset(update_id)
        return handled

    def run_forever(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                LOGGER.exception("telegram_control_poll_failed")
                self._stop.wait(2.0)

    def stop(self) -> None:
        self._stop.set()
