from __future__ import annotations

import os
import signal
import threading
from datetime import UTC, datetime
from pathlib import Path

from signal_bot.operator_control import SignalBotOperatorControl
from signal_bot.operator_http import SignalBotControlHttpServer
from signal_bot.operator_ledger import SignalBotControlLedger


class CountingSender:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.Lock()
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text("0\n", encoding="utf-8")

    def send_test_notification(self) -> None:
        with self._lock:
            current = int(self._path.read_text(encoding="utf-8").strip())
            self._path.write_text(f"{current + 1}\n", encoding="utf-8")


def required_environment(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def main() -> int:
    port = int(required_environment("MODULAR_SIGNAL_BOT_E2E_PORT"))
    state_path = Path(required_environment("MODULAR_SIGNAL_BOT_E2E_STATE_PATH"))
    send_count_path = Path(required_environment("MODULAR_SIGNAL_BOT_E2E_SEND_COUNT_PATH"))
    reader_token = required_environment("MODULAR_SIGNAL_BOT_E2E_READER_TOKEN")
    operator_token = required_environment("MODULAR_SIGNAL_BOT_E2E_OPERATOR_TOKEN")

    ledger = SignalBotControlLedger(state_path)
    sender = CountingSender(send_count_path)
    control = SignalBotOperatorControl(
        ledger=ledger,
        health=lambda: {
            "status": "ok",
            "cycles": 17,
            "signals_sent": 3,
            "last_success_at": datetime(2026, 8, 9, 10, 45, tzinfo=UTC),
        },
        notifications_enabled=lambda: True,
        sender=sender,
        configured=True,
    )
    server = SignalBotControlHttpServer(
        control,
        reader_token=reader_token,
        operator_token=operator_token,
        host="127.0.0.1",
        port=port,
    )

    stopping = threading.Event()

    def stop(_signum: int, _frame: object) -> None:
        stopping.set()

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    server.start()
    print(f"Signal Bot owner control E2E listening on 127.0.0.1:{port}", flush=True)
    try:
        stopping.wait()
    finally:
        server.close()
        ledger.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
