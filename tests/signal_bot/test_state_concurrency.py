from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from signal_bot.state import SQLiteStateStore


def test_sqlite_preferences_cursor_and_offset_are_thread_safe(tmp_path: Path) -> None:
    state = SQLiteStateStore(tmp_path / "concurrency.db")

    def writer(index: int) -> None:
        state.set_notifications("42", index % 2 == 0)
        state.set_last_processed("BTCUSDT", f"{index + 1}m", datetime.now(timezone.utc))
        state.set_telegram_update_offset(index + 1)
        state.notifications_enabled("42")
        state.telegram_update_offset()

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(writer, range(100)))
    assert state.telegram_update_offset() == 100
    assert state.last_processed("BTCUSDT", "100m") is not None
    state.close()
