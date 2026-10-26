from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from modular_core import Side
from signal_bot.domain import EntryType, SignalStatus, TradeSignal
from signal_bot.measurement import snapshot_sqlite_state
from signal_bot.state import SQLiteStateStore, StateCapacityError

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def make_signal(index: int) -> TradeSignal:
    return TradeSignal(
        signal_id=f"concurrent_{index:032x}",
        strategy_id="wave2-capacity",
        strategy_version="2.0.0",
        symbol="BTCUSDT",
        market="binance_usdt_m_perpetual",
        side=Side.BUY,
        signal_timestamp=NOW + timedelta(seconds=index),
        signal_timeframe="5m",
        entry_type=EntryType.LIMIT,
        entry_price=Decimal("100.0"),
        stop_loss=Decimal("99.0"),
        take_profit_levels=(Decimal("102.0"),),
        invalidation_price=None,
        risk_reward=(Decimal("2.0"),),
        reason_codes=("capacity_race",),
        human_readable_reason="atomic cross-connection capacity test",
        expires_at=NOW + timedelta(hours=1),
        setup_id=f"capacity-{index}",
        metadata={"sequence": index},
        chart_enabled=False,
        source="core",
        contract_version="1.0",
    )


def admit_at_barrier(
    store: SQLiteStateStore,
    signal: TradeSignal,
    barrier: threading.Barrier,
) -> bool:
    barrier.wait(timeout=5)
    try:
        return store.register(signal)
    except StateCapacityError:
        return False


def transition_at_barrier(
    store: SQLiteStateStore,
    signal_id: str,
    barrier: threading.Barrier,
) -> None:
    barrier.wait(timeout=5)
    store.transition(signal_id, SignalStatus.CANCELLED)


def test_two_connections_cannot_over_admit_same_capacity(tmp_path: Path) -> None:
    database = tmp_path / "admission-race.sqlite3"
    first = SQLiteStateStore(database, max_total_signals=1, max_pending_signals=1)
    second = SQLiteStateStore(database, max_total_signals=1, max_pending_signals=1)
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        first_future = executor.submit(admit_at_barrier, first, make_signal(1), barrier)
        second_future = executor.submit(admit_at_barrier, second, make_signal(2), barrier)
        outcomes = (first_future.result(timeout=10), second_future.result(timeout=10))

    assert sorted(outcomes) == [False, True]
    snapshot = snapshot_sqlite_state(database)
    assert snapshot.total_signals == 1
    assert snapshot.pending_signals == 1
    first.close()
    second.close()


def test_concurrent_terminal_transition_decrements_pending_once(tmp_path: Path) -> None:
    database = tmp_path / "transition-race.sqlite3"
    first = SQLiteStateStore(database, max_total_signals=2, max_pending_signals=2)
    signal = make_signal(3)
    assert first.register(signal)
    second = SQLiteStateStore(database, max_total_signals=2, max_pending_signals=2)
    barrier = threading.Barrier(2)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = (
            executor.submit(transition_at_barrier, first, signal.signal_id, barrier),
            executor.submit(transition_at_barrier, second, signal.signal_id, barrier),
        )
        for future in futures:
            future.result(timeout=10)

    snapshot = snapshot_sqlite_state(database)
    assert snapshot.total_signals == 1
    assert snapshot.pending_signals == 0
    assert snapshot.terminal_signals == 1
    first.close()
    second.close()
